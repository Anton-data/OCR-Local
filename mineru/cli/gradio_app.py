# Copyright (c) Opendatalab. All rights reserved.

import asyncio
import html as html_lib
import httpx
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import click
import gradio as gr
from gradio_pdf import PDF
from loguru import logger

os.environ["TORCH_CUDNN_V8_API_DISABLED"] = "1"
# 检测 Gradio 版本，用于兼容 Gradio 5 和 Gradio 6
_gradio_major_version = int(gr.__version__.split('.')[0])
IS_GRADIO_6 = _gradio_major_version >= 6

log_level = os.getenv("MINERU_LOG_LEVEL", "INFO").upper()
logger.remove()  # 移除默认handler
logger.add(sys.stderr, level=log_level)  # 添加新handler

from mineru.cli.common import (
    image_suffixes,
    normalize_task_stem,
    office_suffixes,
    pdf_suffixes,
    read_fn,
)
from mineru.cli import api_client as _api_client
from mineru.cli.backend_options import (
    DEFAULT_BACKEND,
    DEFAULT_HYBRID_EFFORT,
    HYBRID_EFFORT_CHOICES,
    HTTP_CLIENT_BACKEND_CHOICES,
    LOCAL_BACKEND_CHOICES,
)
from mineru.cli.client_side_output import (
    regenerate_client_side_outputs,
    regenerate_derived_outputs,
)
from mineru.cli.output_paths import resolve_parse_dir
from mineru.cli.vlm_preload import resolve_gradio_local_api_cli_args
from mineru.cli.visualization import VisualizationJob, run_visualization_job
from mineru.cli import visual_editor
from mineru.utils.docx_export import export_docx_from_result_dir
from mineru.utils.ocr_language import PUBLIC_OCR_LANGUAGE_CHOICES
from mineru.utils.searchable_pdf import export_searchable_pdf_from_result_dir

_gradio_local_api_server = _api_client.ReusableLocalAPIServer()


@dataclass(frozen=True)
class GradioConcurrencyWaitSnapshot:
    limit: int
    active: int
    waiting: int
    ahead: int


@dataclass
class _LimiterState:
    semaphore: asyncio.Semaphore
    active: int = 0
    waiters: list[object] = field(default_factory=list)


class GradioRequestConcurrencyLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._states: dict[int, _LimiterState] = {}

    def _get_state(self, limit: int):
        if limit <= 0:
            return None
        with self._lock:
            state = self._states.get(limit)
            if state is None:
                state = _LimiterState(semaphore=asyncio.Semaphore(limit))
                self._states[limit] = state
            return state

    def _build_wait_snapshot(
        self,
        state: _LimiterState,
        limit: int,
        wait_token: object,
    ) -> GradioConcurrencyWaitSnapshot | None:
        if wait_token not in state.waiters:
            return None

        return GradioConcurrencyWaitSnapshot(
            limit=limit,
            active=state.active,
            waiting=len(state.waiters),
            ahead=state.waiters.index(wait_token),
        )

    def _remove_waiter(self, state: _LimiterState, wait_token: object) -> None:
        if wait_token in state.waiters:
            state.waiters.remove(wait_token)

    async def _cleanup_acquire_interruption(
        self,
        state: _LimiterState,
        acquire_task: asyncio.Task[bool],
        wait_token: object,
        should_wait: bool,
    ) -> None:
        if not acquire_task.done():
            acquire_task.cancel()
            await asyncio.gather(acquire_task, return_exceptions=True)
        elif not acquire_task.cancelled():
            try:
                acquired = acquire_task.result()
            except Exception:
                acquired = False
            if acquired:
                state.semaphore.release()

        if should_wait:
            with self._lock:
                self._remove_waiter(state, wait_token)

    @asynccontextmanager
    async def acquire(
        self,
        limit: int,
        on_wait: Callable[[GradioConcurrencyWaitSnapshot], None] | None = None,
    ):
        state = self._get_state(limit)
        if state is None:
            yield
            return

        wait_token = object()
        should_wait = False
        snapshot = None
        with self._lock:
            if state.active >= limit or state.waiters:
                state.waiters.append(wait_token)
                should_wait = True
                snapshot = self._build_wait_snapshot(state, limit, wait_token)

        acquire_task: asyncio.Task[bool] = asyncio.create_task(state.semaphore.acquire())
        last_wait_ahead = None
        if should_wait and on_wait is not None and snapshot is not None:
            on_wait(snapshot)
            last_wait_ahead = snapshot.ahead

        try:
            if should_wait:
                while True:
                    done, _ = await asyncio.wait(
                        {acquire_task},
                        timeout=STATUS_TIMER_INTERVAL_SECONDS,
                    )
                    if acquire_task in done:
                        acquire_task.result()
                        break

                    if on_wait is None:
                        continue

                    with self._lock:
                        snapshot = self._build_wait_snapshot(state, limit, wait_token)

                    if snapshot is None or snapshot.ahead == last_wait_ahead:
                        continue

                    on_wait(snapshot)
                    last_wait_ahead = snapshot.ahead
            else:
                await acquire_task
        except BaseException:
            await self._cleanup_acquire_interruption(
                state=state,
                acquire_task=acquire_task,
                wait_token=wait_token,
                should_wait=should_wait,
            )
            raise

        with self._lock:
            if should_wait:
                self._remove_waiter(state, wait_token)
            state.active += 1
        try:
            yield
        finally:
            with self._lock:
                state.active = max(0, state.active - 1)
            state.semaphore.release()


_gradio_request_concurrency_limiter = GradioRequestConcurrencyLimiter()

STATUS_BOX_AUTOSCROLL_JS = """
(value) => {
    const scrollToBottom = () => {
        const textarea = document.querySelector(".convert-status-box textarea");
        if (!textarea) {
            return;
        }
        textarea.scrollTop = textarea.scrollHeight;
    };

    requestAnimationFrame(() => {
        scrollToBottom();
        requestAnimationFrame(scrollToBottom);
    });

    return [];
}
"""

STATUS_TIMER_INTERVAL_SECONDS = 0.1
STATUS_QUEUE_ANIMATION_INTERVAL_SECONDS = 1.0
STATUS_QUEUE_ANIMATION_MAX_DOTS = 10

STATUS_PREPARING_REQUEST = "Preparing request..."
STATUS_CHECKING_SERVER = "Checking server status..."
STATUS_SUBMITTING_TASK = "Submitting task..."
STATUS_DOWNLOADING_RESULT = "Task completed, downloading result..."
STATUS_PROCESSING_OUTPUT = "Preparing outputs..."
STATUS_COMPLETED = "Completed"
STATUS_QUEUED_ON_SERVER = "Queued on server"
STATUS_PROCESSING_ON_SERVER = "Processing on server"
STATUS_QUEUED_LOCALLY_PREFIX = "Queued locally:"

BACKEND_CHOICE_DEFINITIONS = list(LOCAL_BACKEND_CHOICES)
HTTP_CLIENT_BACKEND_CHOICE_DEFINITIONS = list(HTTP_CLIENT_BACKEND_CHOICES)
STATUS_STEP_DEFINITIONS = [
    ("status_step_prepare", STATUS_PREPARING_REQUEST),
    ("status_step_check", STATUS_CHECKING_SERVER),
    ("status_step_submit", STATUS_SUBMITTING_TASK),
    ("status_step_queue", STATUS_QUEUED_ON_SERVER),
    ("status_step_process", STATUS_PROCESSING_ON_SERVER),
    ("status_step_download", STATUS_DOWNLOADING_RESULT),
    ("status_step_outputs", STATUS_PROCESSING_OUTPUT),
    ("status_step_done", STATUS_COMPLETED),
]


def normalize_mineru_locale(locale):
    """统一自定义 HTML 的语言归一规则，俄语和乌克兰语分别保留。"""
    normalized = str(locale or "").strip().lower()
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("ru"):
        return "ru"
    if normalized.startswith("uk"):
        return "uk"
    return "en"


def resolve_i18n_text(i18n, key, locale=None):
    """按指定语言读取自定义 HTML 需要的纯文本文案，避免直接渲染 Gradio I18nData 元数据。"""
    if i18n is None:
        return key
    translations = getattr(i18n, "translations", None)
    if translations:
        preferred_locale = normalize_mineru_locale(
            locale or os.getenv("MINERU_GRADIO_DEFAULT_LOCALE", "ru")
        )
        preferred_text = translations.get(preferred_locale, {}).get(key)
        if preferred_text is not None:
            return preferred_text
        fallback_text = translations.get("en", {}).get(key)
        if fallback_text is not None:
            return fallback_text
        return key
    return i18n(key)


def translate_ui(i18n, key, locale=None):
    """按目标语言读取纯文本文案，用作自定义 HTML 的初始渲染内容。"""
    return resolve_i18n_text(i18n, key, locale)


def resolve_request_locale(request):
    """根据 Gradio 请求头推断浏览器语言，避免流式状态面板高频刷新时闪回默认语言。"""
    headers = getattr(request, "headers", None) or {}
    if not hasattr(headers, "get"):
        return None

    accept_language = headers.get("accept-language") or headers.get("Accept-Language")
    if not accept_language:
        return None

    language_candidates = []
    for order, raw_item in enumerate(str(accept_language).split(",")):
        parts = [part.strip() for part in raw_item.split(";") if part.strip()]
        if not parts:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                break
        language_candidates.append((-quality, order, parts[0]))

    if not language_candidates:
        return None
    _, _, preferred_language = min(language_candidates)
    return normalize_mineru_locale(preferred_language)


def build_client_i18n_attrs(i18n, key):
    """为自定义 HTML 输出中英文文案属性，交给前端按浏览器语言切换。"""
    attrs = [f'data-mineru-i18n-key="{html_lib.escape(key, quote=True)}"']
    for locale in ("en", "zh", "ru", "uk"):
        text = resolve_i18n_text(i18n, key, locale)
        attrs.append(f'data-mineru-i18n-{locale}="{html_lib.escape(text, quote=True)}"')
    return " ".join(attrs)


def render_client_i18n_text(i18n, key, locale=None):
    """生成可被前端重新本地化的文本节点，避免 header/status 在英文环境固定成中文。"""
    return (
        f"<span {build_client_i18n_attrs(i18n, key)}>"
        f"{html_lib.escape(translate_ui(i18n, key, locale))}"
        "</span>"
    )


def build_backend_choices(http_client_enable, i18n):
    """构建后端选项列表，展示文案与提交给后端的 backend 值保持完全一致。"""
    choices = list(BACKEND_CHOICE_DEFINITIONS)
    if http_client_enable:
        choices.extend(HTTP_CLIENT_BACKEND_CHOICE_DEFINITIONS)
    return choices


def is_http_client_backend(backend_choice):
    """判断当前后端是否为 http-client 类型，用于控制服务器地址配置显隐。"""
    return isinstance(backend_choice, str) and backend_choice.endswith("-http-client")


def select_backend_info_key(backend_choice):
    """根据解析后端选择说明文案的 i18n key。"""
    if not isinstance(backend_choice, str):
        return "backend_info_default"
    if backend_choice.startswith("vlm"):
        return "backend_info_vlm"
    if backend_choice == "pipeline":
        return "backend_info_pipeline"
    if backend_choice.startswith("hybrid"):
        return "backend_info_hybrid"
    return "backend_info_default"


def select_force_ocr_info_key(backend_choice: object) -> str:
    """根据解析后端选择强制 OCR 说明；只有 pipeline 需要提示 OCR 语言要求。"""
    if isinstance(backend_choice, str) and backend_choice.startswith("hybrid"):
        return "force_ocr_info_hybrid"
    return "force_ocr_info"


def is_effort_option_visible(backend_choice):
    """判断当前后端是否需要展示 Hybrid effort 配置。"""
    return isinstance(backend_choice, str) and backend_choice.startswith("hybrid")


def resolve_status_step_index(status_lines):
    """根据现有状态日志推断步骤面板中当前应高亮的步骤索引。"""
    if not status_lines:
        return -1, False
    if status_lines[-1].startswith("Failed:"):
        return len(STATUS_STEP_DEFINITIONS) - 1, True
    if any(line.startswith(STATUS_COMPLETED) for line in status_lines):
        return len(STATUS_STEP_DEFINITIONS) - 1, False

    for index in range(len(STATUS_STEP_DEFINITIONS) - 1, -1, -1):
        _, marker = STATUS_STEP_DEFINITIONS[index]
        if marker == STATUS_QUEUED_ON_SERVER:
            if any(StatusPanelState.is_queue_message(line) for line in status_lines):
                return index, False
            continue
        if any(line.startswith(marker) for line in status_lines):
            return index, False
    return 0, False


def render_status_steps_html(status_text, i18n, locale=None):
    """把流式状态日志渲染为步骤式状态面板，底层日志格式保持不变。"""
    status_lines = [line for line in str(status_text or "").splitlines() if line]
    current_index, is_failed = resolve_status_step_index(status_lines)
    latest_status = (
        status_lines[-1]
        if status_lines
        else render_client_i18n_text(i18n, "status_idle_hint", locale)
    )

    step_items = []
    for index, (label_key, _) in enumerate(STATUS_STEP_DEFINITIONS):
        classes = ["status-step"]
        if is_failed and index == current_index:
            classes.extend(["is-active", "is-error"])
            label = render_client_i18n_text(i18n, "status_step_failed", locale)
        elif index < current_index or (
            current_index == len(STATUS_STEP_DEFINITIONS) - 1 and not is_failed
        ):
            classes.append("is-done")
            label = render_client_i18n_text(i18n, label_key, locale)
        elif index == current_index:
            classes.append("is-active")
            label = render_client_i18n_text(i18n, label_key, locale)
        else:
            classes.append("is-pending")
            label = render_client_i18n_text(i18n, label_key, locale)
        step_items.append(
            f'<div class="{" ".join(classes)}">'
            f'<span class="status-dot"></span>'
            f'<span class="status-label">{label}</span>'
            "</div>"
        )

    title_key = "status_idle_title" if not status_lines else "status_latest"
    return (
        '<div class="status-steps-panel">'
        f'<div class="status-panel-title">'
        f'{render_client_i18n_text(i18n, title_key, locale)}'
        f'</div>'
        f'<div class="status-steps-list">{"".join(step_items)}</div>'
        f'<div class="status-latest">'
        f'{latest_status if not status_lines else html_lib.escape(latest_status)}'
        f'</div>'
        "</div>"
    )


def render_save_status_html(i18n, message_key, locale=None, detail=None, is_error=False):
    """Рендерит компактное сообщение статуса после ручного сохранения правок в status_panel."""
    icon = "⚠️" if is_error else "✅"
    message_span = render_client_i18n_text(i18n, message_key, locale)
    # `detail` уже несёт собственный ведущий разделитель (": " для ошибок,
    # " (" для метки времени), чтобы избежать двойных пробелов при склейке.
    detail_html = html_lib.escape(detail) if detail else ""
    return (
        '<div class="status-steps-panel">'
        f'<div class="status-panel-title">'
        f'{render_client_i18n_text(i18n, "status_latest", locale)}'
        f'</div>'
        f'<div class="status-latest">{icon} {message_span}{detail_html}</div>'
        "</div>"
    )


def render_editor_page_label_html(i18n, current_idx, total_pages, locale=None):
    """Рендерит «Страница N/M» для навигации визуального редактора."""
    label = render_client_i18n_text(i18n, "editor_page_word", locale)
    if not total_pages:
        return f'<span class="mineru-editor-page-label">{label}</span>'
    return (
        f'<span class="mineru-editor-page-label">'
        f'{label} {current_idx + 1} / {total_pages}</span>'
    )


def render_editor_dirty_html(i18n, is_dirty, locale=None):
    """Рендерит индикатор «есть правки, не попавшие в экспорт»; пусто, если не dirty."""
    if not is_dirty:
        return ""
    return (
        '<div class="mineru-editor-dirty-hint">⚠️ '
        f'{render_client_i18n_text(i18n, "editor_exports_dirty", locale)}</div>'
    )


RESOURCE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources')


def load_resource_text(resource_name):
    """读取 mineru/resources 下的文本资源，集中管理 Gradio 静态片段。"""
    resource_path = os.path.join(RESOURCE_DIR, resource_name)
    with open(resource_path, mode='r', encoding='utf-8') as resource_file:
        return resource_file.read()


APP_CSS = load_resource_text('gradio_app.css')
APP_JS = load_resource_text('gradio_app.js')

# Gradio 6 的 js 参数在部分托管环境里只注入函数文本，使用 head 包装确保页面加载后主动执行。
APP_HEAD = f"""
<script>
window.__mineruUiTranslations = __MINERU_UI_TRANSLATIONS__;
(() => {{
    const storageKey = "mineru-ui-locale";
    let locale = "ru";
    try {{
        const queryLocale = new URLSearchParams(window.location.search).get("lang");
        const storedLocale = localStorage.getItem(storageKey);
        locale = queryLocale === "uk" || (queryLocale !== "ru" && storedLocale === "uk")
            ? "uk"
            : "ru";
        localStorage.setItem(storageKey, locale);
        document.cookie = `${{storageKey}}=${{locale}}; Path=/; Max-Age=31536000; SameSite=Lax`;
    }} catch (_error) {{
        // Private browsing or a restrictive policy can disable localStorage.
    }}
    const defineLocale = (property, value) => {{
        try {{
            Object.defineProperty(navigator, property, {{
                configurable: true,
                get: () => value,
            }});
        }} catch (_error) {{
            // Gradio will use the browser locale if Navigator cannot be overridden.
        }}
    }};
    defineLocale("language", locale);
    defineLocale("languages", [locale]);
    document.documentElement.lang = locale;
}})();
(() => {{
    const installMineruAdvancedPopover = {APP_JS};
    if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", installMineruAdvancedPopover, {{ once: true }});
    }} else {{
        installMineruAdvancedPopover();
    }}
}})();
</script>
"""


@dataclass
class StatusPanelState:
    lines: list[str] = field(default_factory=list)
    processing_index: int | None = None
    processing_started_at: float | None = None
    last_processing_elapsed_seconds: float | None = None
    queue_index: int | None = None
    queue_started_at: float | None = None
    queue_base_message: str | None = None

    def append(self, message: str) -> bool:
        if not message:
            return False

        if self.is_queue_message(message):
            self.finalize_processing()
            return self.update_queue(message)

        if message == STATUS_PROCESSING_ON_SERVER:
            self.finalize_queue()
            return self.start_processing()

        self.finalize_processing()
        self.finalize_queue()
        if message == STATUS_COMPLETED:
            message = format_completed_status(self.last_processing_elapsed_seconds)
        if not self.lines or self.lines[-1] != message:
            self.lines.append(message)
            return True
        return False

    def start_processing(self) -> bool:
        if self.processing_started_at is not None:
            return self.tick_processing()

        self.processing_started_at = time.monotonic()
        self.last_processing_elapsed_seconds = 0.0
        self.processing_index = len(self.lines)
        self.lines.append(format_processing_status(0.0))
        return True

    def tick_processing(self) -> bool:
        if self.processing_started_at is None or self.processing_index is None:
            return False

        elapsed_seconds = max(0.0, time.monotonic() - self.processing_started_at)
        self.last_processing_elapsed_seconds = elapsed_seconds
        updated = format_processing_status(elapsed_seconds)
        if self.lines[self.processing_index] != updated:
            self.lines[self.processing_index] = updated
            return True
        return False

    def finalize_processing(self) -> bool:
        if self.processing_started_at is None or self.processing_index is None:
            return False

        self.tick_processing()
        self.processing_started_at = None
        self.processing_index = None
        return True

    def update_queue(self, message: str) -> bool:
        if (
            self.queue_index is None
            or self.queue_started_at is None
            or self.queue_base_message is None
        ):
            self.queue_started_at = time.monotonic()
            self.queue_index = len(self.lines)
            self.queue_base_message = message
            self.lines.append(format_queue_status(message, 0.0))
            return True

        self.queue_base_message = message
        updated = format_queue_status(
            message,
            max(0.0, time.monotonic() - self.queue_started_at),
        )
        if self.lines[self.queue_index] != updated:
            self.lines[self.queue_index] = updated
            return True
        return False

    def tick_queue(self) -> bool:
        if (
            self.queue_index is None
            or self.queue_started_at is None
            or self.queue_base_message is None
        ):
            return False

        updated = format_queue_status(
            self.queue_base_message,
            max(0.0, time.monotonic() - self.queue_started_at),
        )
        if self.lines[self.queue_index] != updated:
            self.lines[self.queue_index] = updated
            return True
        return False

    def finalize_queue(self) -> bool:
        if (
            self.queue_index is None
            or self.queue_started_at is None
            or self.queue_base_message is None
        ):
            return False

        self.tick_queue()
        self.queue_index = None
        self.queue_started_at = None
        self.queue_base_message = None
        return True

    def tick(self) -> bool:
        if self.is_processing:
            return self.tick_processing()
        if self.is_queueing:
            return self.tick_queue()
        return False

    @property
    def is_processing(self) -> bool:
        return self.processing_started_at is not None

    @property
    def is_queueing(self) -> bool:
        return self.queue_started_at is not None

    @property
    def animation_interval_seconds(self) -> float | None:
        if self.is_processing:
            return STATUS_TIMER_INTERVAL_SECONDS
        if self.is_queueing:
            return STATUS_QUEUE_ANIMATION_INTERVAL_SECONDS
        return None

    @staticmethod
    def is_queue_message(message: str) -> bool:
        return (
            message.startswith(STATUS_QUEUED_LOCALLY_PREFIX)
            or message.startswith(STATUS_QUEUED_ON_SERVER)
        )

    def render(self) -> str:
        return "\n".join(self.lines)


def format_failed_status(error: Exception | str) -> str:
    return f"Failed: {error}"


def format_processing_status(elapsed_seconds: float) -> str:
    return f"{STATUS_PROCESSING_ON_SERVER} ({elapsed_seconds:.1f}s)"


def format_completed_status(elapsed_seconds: float | None) -> str:
    """生成完成状态文案，保留服务端解析阶段最终耗时。"""
    if elapsed_seconds is None:
        return STATUS_COMPLETED
    return f"{STATUS_COMPLETED} ({elapsed_seconds:.1f}s)"


def format_queue_status(base_message: str, elapsed_seconds: float) -> str:
    dots = "." * (
        (int(max(0.0, elapsed_seconds)) % STATUS_QUEUE_ANIMATION_MAX_DOTS) + 1
    )
    return f"{base_message}{dots}"


def format_concurrency_wait_message(snapshot: GradioConcurrencyWaitSnapshot) -> str:
    return f"{STATUS_QUEUED_LOCALLY_PREFIX} {snapshot.ahead} request(s) ahead"


def format_remote_status_message(
    status_snapshot: _api_client.TaskStatusSnapshot | str,
) -> str:
    if isinstance(status_snapshot, _api_client.TaskStatusSnapshot):
        status = status_snapshot.status
        queued_ahead = status_snapshot.queued_ahead
    else:
        status = status_snapshot
        queued_ahead = None

    if status == "pending":
        if queued_ahead is not None:
            return f"{STATUS_QUEUED_ON_SERVER}: {queued_ahead} request(s) ahead"
        return STATUS_QUEUED_ON_SERVER
    if status == "processing":
        return STATUS_PROCESSING_ON_SERVER
    if status == "completed":
        return STATUS_COMPLETED
    if status == "failed":
        return format_failed_status("server task failed")
    return f"Task status: {status}"


def compress_directory_to_zip(directory_path, output_zip_path):
    """压缩指定目录到一个 ZIP 文件。

    :param directory_path: 要压缩的目录路径
    :param output_zip_path: 输出的 ZIP 文件路径
    """
    try:
        with zipfile.ZipFile(output_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:

            # 遍历目录中的所有文件和子目录
            for root, dirs, files in os.walk(directory_path):
                for file in files:
                    # 构建完整的文件路径
                    file_path = os.path.join(root, file)
                    # 计算相对路径
                    arcname = os.path.relpath(file_path, directory_path)
                    # 添加文件到 ZIP 文件
                    zipf.write(file_path, arcname)
        return 0
    except Exception as e:
        logger.exception(e)
        return -1


GRADIO_PREVIEW_IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
}
GRADIO_PREVIEW_EXTERNAL_SRC_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _resolve_gradio_preview_image_path(src, image_dir_path):
    """将 Markdown/HTML 中的图片路径解析成 Gradio 文件路由需要的本地完整路径。"""
    image_src = str(src).strip()
    if not image_src:
        return None
    if Path(image_src).suffix.lower() not in GRADIO_PREVIEW_IMAGE_SUFFIXES:
        return None

    resolved_path = (Path(image_dir_path) / image_src).resolve(strict=False)
    if not resolved_path.is_file():
        logger.warning(f"Skip missing Gradio preview image: {resolved_path}")
        return None
    return str(resolved_path)


def replace_image_with_gradio_file_urls(markdown_text, image_dir_path):
    """将 Gradio 预览中的本地图片路径改写为 HTTP 文件链接，不修改导出的 Markdown 文件。"""
    if not isinstance(markdown_text, str) or not image_dir_path:
        return markdown_text

    def _path_to_public_url(image_src):
        image_path = _resolve_gradio_preview_image_path(image_src, image_dir_path)
        if not image_path:
            return None
        return f"/gradio_api/file={quote(image_path, safe='/:')}"

    # 匹配 Markdown 正文图片 ![alt](path)，只替换可安全访问的本地图片路径。
    def replace_md(match):
        alt_text = match.group("alt")
        image_src = match.group("src")
        public_url = _path_to_public_url(image_src)
        if public_url:
            return f"![{alt_text}]({public_url})"
        return match.group(0)

    result = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)",
        replace_md,
        markdown_text,
    )

    # 匹配 HTML 表格内的 <img src="path">，跳过 data/http/blob 等已有可访问地址。
    def replace_html_src(match):
        prefix = match.group("prefix")
        quote_char = match.group("quote")
        image_src = match.group("src")
        public_url = _path_to_public_url(image_src)
        if public_url:
            return f"{prefix}{quote_char}{public_url}{quote_char}"
        return match.group(0)

    result = re.sub(
        r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<quote>[\"'])(?P<src>[^\"']+)(?P=quote)",
        replace_html_src,
        result,
        flags=re.IGNORECASE,
    )

    return result


def read_gradio_content_list_json(local_md_dir, file_name):
    """读取本次 Gradio 解析目录中的 legacy content_list JSON，失败时返回空字符串。"""
    content_list_path = Path(local_md_dir) / f"{file_name}_content_list.json"
    if not content_list_path.is_file():
        logger.warning(
            f"Content list JSON not found for Gradio preview: {content_list_path}"
        )
        return ""
    try:
        return content_list_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning(
            f"Failed to read Gradio content list JSON: {content_list_path}, error={exc}"
        )
        return ""


def build_gradio_docx_result(local_md_dir, file_name):
    """Формирует DOCX-файл из content_list.json текущего результата Gradio.

    Возвращает путь к файлу или None, если content_list.json отсутствует
    или экспорт не удался — ошибка экспорта DOCX не должна ломать основной
    процесс конвертации.
    """
    content_list_path = Path(local_md_dir) / f"{file_name}_content_list.json"
    if not content_list_path.is_file():
        return None
    try:
        docx_output_path = Path(local_md_dir) / f"{file_name}.docx"
        return str(export_docx_from_result_dir(local_md_dir, docx_output_path))
    except Exception as exc:
        logger.warning(f"Failed to export DOCX for {file_name}: {exc}")
        return None


def build_gradio_searchable_pdf_result(local_md_dir, file_name):
    """Формирует «PDF с текстовым слоем» (searchable PDF) для текущего результата Gradio.

    Возвращает путь к файлу или None, если *_middle.json/*_origin.pdf отсутствуют
    или экспорт не удался — ошибка экспорта не должна ломать основной процесс
    конвертации.
    """
    middle_json_path = Path(local_md_dir) / f"{file_name}_middle.json"
    if not middle_json_path.is_file():
        return None
    try:
        searchable_pdf_output_path = Path(local_md_dir) / f"{file_name}_searchable.pdf"
        return str(
            export_searchable_pdf_from_result_dir(
                local_md_dir, output_path=searchable_pdf_output_path
            )
        )
    except Exception as exc:
        logger.warning(f"Failed to export searchable PDF for {file_name}: {exc}")
        return None


def find_soffice_executable():
    """Возвращает путь к soffice/libreoffice или None, если LibreOffice недоступен."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_docx_to_pdf_for_preview(docx_path, timeout_seconds=180):
    """Конвертирует итоговый DOCX в PDF для локального превью (LibreOffice headless).

    PDF кладётся в подпапку docx_preview рядом с DOCX, чтобы не конфликтовать
    с файлами результата. Возвращает путь к PDF или None; любая ошибка
    конвертации не должна ломать основной пайплайн — только warning в лог.
    """
    profile_dir = None
    try:
        docx_file = Path(docx_path)
        if not docx_file.is_file():
            return None
        soffice = find_soffice_executable()
        if soffice is None:
            logger.warning(
                "LibreOffice (soffice) is not available; "
                "DOCX preview falls back to the online viewer"
            )
            return None
        preview_dir = docx_file.parent / "docx_preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = preview_dir / f"{docx_file.stem}.pdf"
        # Изолированный профиль на вызов: параллельные конвертации LibreOffice
        # с общим профилем конфликтуют ("another instance is running").
        profile_dir = tempfile.mkdtemp(prefix="soffice-profile-")
        profile_url = f"file://{Path(profile_dir).as_posix()}"
        result = subprocess.run(
            [
                soffice,
                "--headless",
                f"-env:UserInstallation={profile_url}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(preview_dir),
                str(docx_file),
            ],
            capture_output=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0 or not pdf_path.is_file():
            stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
            logger.warning(
                f"DOCX preview conversion failed for {docx_path}: "
                f"rc={result.returncode}, stderr={stderr_text[:500]}"
            )
            return None
        return str(pdf_path)
    except Exception as exc:
        logger.warning(f"Failed to convert DOCX to PDF preview for {docx_path}: {exc}")
        return None
    finally:
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)


def _escape_latex_html_chars_for_gradio(content):
    """转义公式内部会被 Gradio HTML 解析链路误判的尖括号，保留 LaTeX 对齐用 &。"""
    return content.replace("<", "&lt;").replace(">", "&gt;")


def escape_latex_blocks_for_gradio_preview(markdown_text, latex_delimiters):
    """根据当前 LaTeX 分隔符，仅转义公式内容，避免影响公式外 Markdown/HTML。"""
    if not markdown_text or not latex_delimiters:
        return markdown_text

    delimiter_pairs = []
    for delimiter in latex_delimiters:
        left = delimiter.get("left")
        right = delimiter.get("right")
        if left and right:
            delimiter_pairs.append((left, right))
    delimiter_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    if not delimiter_pairs:
        return markdown_text

    result = []
    position = 0
    text_length = len(markdown_text)
    while position < text_length:
        matched_pair = None
        for left, right in delimiter_pairs:
            if markdown_text.startswith(left, position):
                matched_pair = (left, right)
                break

        if matched_pair is None:
            result.append(markdown_text[position])
            position += 1
            continue

        left, right = matched_pair
        content_start = position + len(left)
        content_end = markdown_text.find(right, content_start)
        if content_end == -1:
            # 未闭合的分隔符保持原样，并继续扫描后续可能闭合的公式块。
            result.append(markdown_text[position])
            position += 1
            continue

        result.append(left)
        result.append(
            _escape_latex_html_chars_for_gradio(markdown_text[content_start:content_end])
        )
        result.append(right)
        position = content_end + len(right)

    return "".join(result)


def prepare_markdown_for_gradio_preview(markdown_text, latex_delimiters):
    """准备传给 gr.Markdown 的预览文本；原始 Markdown 文件内容不在这里改写。"""
    if not isinstance(markdown_text, str):
        return markdown_text
    return escape_latex_blocks_for_gradio_preview(markdown_text, latex_delimiters)


def normalize_language(language):
    if '(' in language and ')' in language:
        return language.split('(')[0].strip()
    return language


def resolve_parse_method(file_path, is_ocr, backend):
    file_suffix = Path(file_path).suffix.lower().lstrip('.')
    if file_suffix in office_suffixes:
        return "auto"
    if backend.startswith("vlm"):
        return "auto"
    return "ocr" if is_ocr else "auto"


def is_image_analysis_option_visible(backend, effort=DEFAULT_HYBRID_EFFORT):
    """判断 Gradio 图片分析开关是否应展示；Hybrid medium 会强制关闭该功能。"""
    if not isinstance(backend, str):
        return False
    if backend.startswith("vlm"):
        return True
    if backend.startswith("hybrid"):
        return effort == "high"
    return False


def is_ocr_language_option_visible(backend: object) -> bool:
    """判断 OCR 语言选项是否展示；lang 参数只对 pipeline 后端生效。"""
    return backend == "pipeline"


def is_force_ocr_option_visible(backend: object) -> bool:
    """判断强制 OCR 开关是否展示；Hybrid 不需要 lang，但仍支持强制 OCR。"""
    if not isinstance(backend, str):
        return False
    return backend == "pipeline" or backend.startswith("hybrid")


def frontend_managed_initial_visibility(is_visible: bool):
    """转换前端托管显隐控件的初始状态；hidden 会保留 DOM 挂载，避免后续重新挂载。"""
    return True if is_visible else "hidden"


def should_use_client_side_output_generation(client_side_output_generation):
    """判断当前 Gradio 任务是否需要在客户端生成最终输出。"""
    return client_side_output_generation


def create_gradio_run_paths(file_path, output_root="./output"):
    run_id = f"{time.strftime('%y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_stem(Path(file_path).stem)}"
    run_root = Path(output_root) / "gradio" / run_id
    extract_root = run_root / "result"
    archive_zip_path = run_root / f"{safe_stem(Path(file_path).stem)}.zip"
    return run_root, extract_root, archive_zip_path


def build_gradio_allowed_paths(output_root="./output"):
    """生成 Gradio 可公开访问目录，确保预览 HTTP 图片链接能被 /gradio_api/file= 读取。"""
    allowed_paths = []
    for item in os.environ.get("GRADIO_ALLOWED_PATHS", "").split(","):
        item = item.strip()
        if item:
            allowed_paths.append(item)

    output_path = str(Path(output_root).resolve())
    if output_path not in allowed_paths:
        allowed_paths.append(output_path)
    return allowed_paths


def build_gradio_upload_name(file_path):
    path = Path(file_path)
    return f"{normalize_task_stem(path.stem)}{path.suffix}"


_BATCH_INPUT_SUFFIXES = frozenset(
    f".{suffix.lower()}" for suffix in pdf_suffixes + image_suffixes + office_suffixes
)
_BATCH_RESULT_DIR_SUFFIX = "_MinerU"


def resolve_batch_source_directory(source_folder: str) -> Path:
    """Resolve a user-entered Windows source folder inside the Docker mount.

    Browser uploads deliberately expose only a temporary server-side copy, so
    saving a result beside the *original* is possible only for a folder that
    the local Gradio container can access directly. ``docker/compose.dev.yaml``
    maps the configured host drive to ``MINERU_BATCH_HOST_ROOT``.
    """
    raw_path = (source_folder or "").strip().strip('"')
    if not raw_path:
        raise ValueError("Source folder is not specified")

    host_root = Path(os.getenv("MINERU_BATCH_HOST_ROOT", "/host-d")).resolve()
    host_drive = os.getenv("MINERU_BATCH_SOURCE_DRIVE", "D").upper()
    windows_match = re.fullmatch(r"([a-zA-Z]):[\\/](.*)", raw_path)
    if windows_match:
        drive, relative_path = windows_match.groups()
        if drive.upper() != host_drive:
            raise ValueError(f"Only the {host_drive}: drive is available for batch source folders")
        candidate = host_root.joinpath(*[part for part in re.split(r"[\\/]+", relative_path) if part])
    else:
        candidate = Path(raw_path)

    resolved = candidate.resolve()
    if not resolved.is_relative_to(host_root):
        raise ValueError("Source folder must be inside the configured local source drive")
    if not resolved.is_dir():
        raise ValueError(f"Source folder does not exist: {raw_path}")
    return resolved


def find_batch_input_files(source_dir: str | Path, recursive: bool = False) -> list[Path]:
    """Return supported source documents while never re-processing MinerU output."""
    source_dir = Path(source_dir)
    iterator = source_dir.rglob("*") if recursive else source_dir.glob("*")
    files = []
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in _BATCH_INPUT_SUFFIXES:
            continue
        if any(part.casefold().endswith(_BATCH_RESULT_DIR_SUFFIX.casefold()) for part in path.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda path: str(path).casefold())


def _unique_batch_result_dir(source_file: Path) -> Path:
    base_dir = source_file.parent / f"{normalize_task_stem(source_file.stem)}{_BATCH_RESULT_DIR_SUFFIX}"
    if not base_dir.exists():
        return base_dir
    for suffix in range(2, 10_000):
        candidate = base_dir.with_name(f"{base_dir.name}_{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create a unique result folder for {source_file.name}")


def save_batch_result_next_to_source(local_md_dir: str | Path, source_file: str | Path) -> Path:
    """Copy a complete parsed-result directory beside its source without overwrite."""
    destination = _unique_batch_result_dir(Path(source_file))
    shutil.copytree(local_md_dir, destination)
    return destination


def create_batch_result_archive(result_dirs: list[Path], archive_path: str | Path) -> Path:
    """Create one portable ZIP containing all successful per-document results."""
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for result_dir in result_dirs:
            for path in result_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(result_dir.parent))
    return archive_path


def resolve_result_file_name(submit_response, extract_root, file_path):
    if submit_response.file_names:
        return submit_response.file_names[0]

    candidate_dirs = sorted(path.name for path in Path(extract_root).iterdir() if path.is_dir())
    if len(candidate_dirs) == 1:
        return candidate_dirs[0]
    return normalize_task_stem(Path(file_path).stem)


async def resolve_server_health(http_client, api_url):
    if api_url:
        return await _api_client.fetch_server_health(
            http_client,
            _api_client.normalize_base_url(api_url),
        )

    local_server, started_now = _gradio_local_api_server.ensure_started()
    if started_now:
        logger.info(f"Started local mineru-api at {local_server.base_url}")
    return await _api_client.wait_for_local_api_ready(http_client, local_server)


async def ensure_local_api_ready_for_gradio_startup(
    timeout_seconds: float = _api_client.LOCAL_API_STARTUP_TIMEOUT_SECONDS,
):
    local_server, started_now = _gradio_local_api_server.ensure_started()
    if started_now:
        logger.info(f"Started local mineru-api at {local_server.base_url}")

    async with httpx.AsyncClient(
        timeout=_api_client.build_http_timeout(),
        follow_redirects=True,
    ) as http_client:
        return await _api_client.wait_for_local_api_ready(
            http_client,
            local_server,
            timeout_seconds=timeout_seconds,
        )


def maybe_prepare_local_api_for_gradio_startup(
    *,
    api_url: str | None,
    enable_vlm_preload: bool,
):
    if api_url is not None or not enable_vlm_preload:
        return None

    try:
        return asyncio.run(ensure_local_api_ready_for_gradio_startup())
    except Exception:
        _gradio_local_api_server.stop()
        raise


def resolve_gradio_max_concurrent_requests(api_url, server_health):
    if api_url is None:
        return server_health.max_concurrent_requests

    return _api_client.resolve_effective_max_concurrent_requests(
        local_max=_api_client.read_max_concurrent_requests(
            default=_api_client.DEFAULT_MAX_CONCURRENT_REQUESTS
        ),
        server_max=server_health.max_concurrent_requests,
    )


def maybe_generate_local_preview(extract_root, file_name, file_suffix, backend, parse_method):
    if file_suffix in office_suffixes:
        return None

    parse_dir = resolve_parse_dir(
        extract_root,
        file_name,
        backend,
        parse_method,
        allow_office_fallback=True,
    )
    visualization_job = VisualizationJob(
        document_stem=file_name,
        backend=backend,
        parse_method=parse_method,
        parse_dir=parse_dir,
        draw_span=backend.startswith("pipeline"),
    )
    result = run_visualization_job(visualization_job)
    if result.status != "finished":
        logger.warning(
            f"Skipping visualization for {visualization_job.document_stem}: {result.message}"
        )
    return resolve_preview_pdf_path(parse_dir, file_name)


async def _run_to_markdown_job(
    file_path,
    end_pages=10,
    is_ocr=False,
    formula_enable=True,
    table_enable=True,
    image_analysis=True,
    effort=DEFAULT_HYBRID_EFFORT,
    language="ch",
    backend="pipeline",
    url=None,
    api_url=None,
    client_side_output_generation=False,
    status_callback: Callable[[str], None] | None = None,
):
    if file_path is None:
        return "", "", "", None, None, None, None, None

    def emit_status(message: str) -> None:
        if status_callback is not None:
            status_callback(message)

    normalized_language = normalize_language(language)
    file_path = str(file_path)
    file_suffix = Path(file_path).suffix.lower().lstrip('.')
    use_client_side_output_generation = should_use_client_side_output_generation(
        client_side_output_generation
    )
    parse_method = resolve_parse_method(file_path, is_ocr, backend)
    run_root, extract_root, archive_zip_path = create_gradio_run_paths(file_path)
    run_root.mkdir(parents=True, exist_ok=True)

    form_data = _api_client.build_parse_request_form_data(
        lang_list=[normalized_language],
        backend=backend,
        effort=effort,
        parse_method=parse_method,
        formula_enable=formula_enable,
        table_enable=table_enable,
        image_analysis=image_analysis,
        server_url=url,
        start_page_id=0,
        end_page_id=end_pages - 1,
        return_md=not use_client_side_output_generation,
        return_middle_json=True,
        return_model_output=True,
        return_content_list=not use_client_side_output_generation,
        return_images=True,
        response_format_zip=True,
        return_original_file=True,
        client_side_output_generation=use_client_side_output_generation,
    )
    upload_assets = [
        _api_client.UploadAsset(
            path=Path(file_path),
            upload_name=build_gradio_upload_name(file_path),
        )
    ]

    async with httpx.AsyncClient(
        timeout=_api_client.build_http_timeout(),
        follow_redirects=True,
    ) as http_client:
        emit_status(STATUS_PREPARING_REQUEST)
        emit_status(STATUS_CHECKING_SERVER)
        server_health = await resolve_server_health(http_client, api_url)
        effective_max_concurrent_requests = resolve_gradio_max_concurrent_requests(
            api_url=api_url,
            server_health=server_health,
        )
        async with _gradio_request_concurrency_limiter.acquire(
            effective_max_concurrent_requests,
            on_wait=lambda snapshot: emit_status(
                format_concurrency_wait_message(snapshot)
            ),
        ):
            emit_status(STATUS_SUBMITTING_TASK)
            submit_response = await _api_client.submit_parse_task(
                base_url=server_health.base_url,
                upload_assets=upload_assets,
                form_data=form_data,
            )
            emit_status(f"Task submitted：task_id={submit_response.task_id}")

            last_task_snapshot = None

            def handle_task_status(
                status_snapshot: _api_client.TaskStatusSnapshot,
            ) -> None:
                nonlocal last_task_snapshot
                if status_snapshot == last_task_snapshot:
                    return
                last_task_snapshot = status_snapshot
                emit_status(format_remote_status_message(status_snapshot))

            await _api_client.wait_for_task_result(
                client=http_client,
                submit_response=submit_response,
                task_label=Path(file_path).name,
                status_snapshot_callback=handle_task_status,
            )
            emit_status(STATUS_DOWNLOADING_RESULT)
            result_zip_path = await _api_client.download_result_zip(
                client=http_client,
                submit_response=submit_response,
                task_label=Path(file_path).name,
            )

    try:
        _api_client.safe_extract_zip(result_zip_path, extract_root)
    finally:
        result_zip_path.unlink(missing_ok=True)

    file_name = resolve_result_file_name(submit_response, extract_root, file_path)
    local_md_dir = resolve_parse_dir(
        extract_root,
        file_name,
        backend,
        parse_method,
        allow_office_fallback=True,
    )
    emit_status(STATUS_PROCESSING_OUTPUT)
    if use_client_side_output_generation:
        await asyncio.to_thread(regenerate_client_side_outputs, local_md_dir, file_name)

    preview_pdf_path = maybe_generate_local_preview(
        extract_root=extract_root,
        file_name=file_name,
        file_suffix=file_suffix,
        backend=backend,
        parse_method=parse_method,
    )

    zip_archive_success = compress_directory_to_zip(local_md_dir, archive_zip_path)
    if zip_archive_success == 0:
        logger.info('Compression successful')
    else:
        logger.error('Compression failed')

    md_path = Path(local_md_dir) / f"{file_name}.md"
    with open(md_path, 'r', encoding='utf-8') as f:
        txt_content = f.read()
    md_content = replace_image_with_gradio_file_urls(txt_content, local_md_dir)
    content_list_json = read_gradio_content_list_json(local_md_dir, file_name)
    docx_path = build_gradio_docx_result(local_md_dir, file_name)

    if file_suffix in office_suffixes:
        preview_pdf_path = None

    emit_status(STATUS_COMPLETED)
    return (
        md_content,
        txt_content,
        content_list_json,
        str(archive_zip_path),
        preview_pdf_path,
        docx_path,
        str(local_md_dir),
        file_name,
    )


async def stream_to_markdown(
    file_path,
    end_pages=10,
    is_ocr=False,
    formula_enable=True,
    table_enable=True,
    image_analysis=True,
    effort=DEFAULT_HYBRID_EFFORT,
    language="ch",
    backend="pipeline",
    url=None,
    api_url=None,
    client_side_output_generation=False,
):
    status_state = StatusPanelState()
    job_task: asyncio.Task | None = None
    queue_get_task: asyncio.Task | None = None
    timer_task: asyncio.Task | None = None
    yield status_state.render(), None, "", "", "", gr.skip(), None, None, None

    if file_path is None:
        return

    status_queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def enqueue_status(message: str) -> None:
        loop.call_soon_threadsafe(status_queue.put_nowait, message)

    try:
        job_task = asyncio.create_task(
            _run_to_markdown_job(
                file_path=file_path,
                end_pages=end_pages,
                is_ocr=is_ocr,
                formula_enable=formula_enable,
                table_enable=table_enable,
                image_analysis=image_analysis,
                effort=effort,
                language=language,
                backend=backend,
                url=url,
                api_url=api_url,
                client_side_output_generation=client_side_output_generation,
                status_callback=enqueue_status,
            )
        )

        while True:
            if job_task.done() and status_queue.empty():
                status_state.finalize_processing()
                status_state.finalize_queue()
                break

            queue_get_task = asyncio.create_task(status_queue.get())
            wait_tasks: set[asyncio.Task] = {job_task, queue_get_task}
            timer_task = None
            animation_interval = status_state.animation_interval_seconds
            if animation_interval is not None:
                timer_task = asyncio.create_task(
                    asyncio.sleep(animation_interval)
                )
                wait_tasks.add(timer_task)

            done, pending = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if queue_get_task in done:
                message = queue_get_task.result()
                if status_state.append(message):
                    yield status_state.render(), None, "", "", "", gr.skip(), None, None, None
            elif timer_task is not None and timer_task in done:
                if status_state.tick():
                    yield status_state.render(), None, "", "", "", gr.skip(), None, None, None
            else:
                queue_get_task.cancel()
                await asyncio.gather(queue_get_task, return_exceptions=True)

            for pending_task in pending:
                if pending_task is job_task:
                    continue
                pending_task.cancel()
                await asyncio.gather(pending_task, return_exceptions=True)
            queue_get_task = None
            timer_task = None

        while not status_queue.empty():
            status_state.append(status_queue.get_nowait())
    except Exception as exc:
        status_state.append(format_failed_status(exc))
        yield status_state.render(), None, "", "", "", gr.skip(), None, None, None
        raise
    finally:
        for task in (queue_get_task, timer_task, job_task):
            if task is None or task.done():
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        (
            md_content,
            txt_content,
            content_list_json,
            archive_zip_path,
            preview_pdf_path,
            docx_path,
            local_md_dir,
            file_name,
        ) = await job_task
    except Exception as exc:
        status_state.append(format_failed_status(exc))
        yield status_state.render(), None, "", "", "", gr.skip(), None, None, None
        raise

    status_state.append(STATUS_COMPLETED)
    yield (
        status_state.render(),
        archive_zip_path,
        md_content,
        txt_content,
        content_list_json,
        preview_pdf_path,
        docx_path,
        local_md_dir,
        file_name,
    )


def resolve_preview_pdf_path(local_md_dir, file_name):
    layout_pdf_path = os.path.join(local_md_dir, file_name + '_layout.pdf')
    if os.path.exists(layout_pdf_path):
        return layout_pdf_path

    origin_pdf_path = os.path.join(local_md_dir, file_name + '_origin.pdf')
    if os.path.exists(origin_pdf_path):
        logger.warning(
            f"Layout preview PDF not found for {file_name}, "
            f"falling back to origin PDF: {origin_pdf_path}"
        )
        return origin_pdf_path

    logger.warning(f"No preview PDF found for {file_name} under {local_md_dir}")
    return None


latex_delimiters_type_a = [
    {'left': '$$', 'right': '$$', 'display': True},
    {'left': '$', 'right': '$', 'display': False},
]
latex_delimiters_type_b = [
    {'left': '\\(', 'right': '\\)', 'display': False},
    {'left': '\\[', 'right': '\\]', 'display': True},
]
latex_delimiters_type_all = latex_delimiters_type_a + latex_delimiters_type_b

header_template = load_resource_text('gradio_header.html')

HEADER_I18N_PLACEHOLDERS = {
    "{{APP_WORKSPACE}}": "app_workspace",
    "{{TOOLS_LABEL}}": "tools_label",
    "{{TOOLS_LABEL_ATTR}}": "tools_label",
    "{{ACTIVE_TOOL}}": "active_tool",
    "{{FUTURE_TOOLS_ATTR}}": "future_tools",
    "{{TOOL_SLOT}}": "tool_slot",
    "{{TOOL_SLOT_HINT}}": "tool_slot_hint",
    "{{LOCAL_MODE}}": "local_mode",
    "{{LANGUAGE_LABEL}}": "language_label",
    "{{LANGUAGE_LABEL_ATTR}}": "language_label",
    "{{HEADER_TITLE}}": "header_title",
    "{{HEADER_SUBTITLE}}": "header_subtitle",
}
HEADER_I18N_ATTRIBUTE_PLACEHOLDERS = {
    "{{TOOLS_LABEL_ATTR}}",
    "{{FUTURE_TOOLS_ATTR}}",
    "{{LANGUAGE_LABEL_ATTR}}",
}
HEADER_GRADIO_VERSION_CLASS_PLACEHOLDER = "{{HEADER_GRADIO_VERSION_CLASS}}"


def render_header_html(i18n):
    """渲染支持 i18n 的中性工具导航侧栏。"""
    rendered_header = header_template
    for placeholder, translation_key in HEADER_I18N_PLACEHOLDERS.items():
        if placeholder in HEADER_I18N_ATTRIBUTE_PLACEHOLDERS:
            replacement = html_lib.escape(translate_ui(i18n, translation_key), quote=True)
        else:
            replacement = render_client_i18n_text(i18n, translation_key)
        rendered_header = rendered_header.replace(
            placeholder,
            replacement,
        )
    rendered_header = rendered_header.replace(
        HEADER_GRADIO_VERSION_CLASS_PLACEHOLDER,
        " mineru-gradio6-header" if IS_GRADIO_6 else "",
    )
    return rendered_header


def render_workspace_intro_html(i18n):
    """Рендерит заголовок активного инструмента над рабочими панелями."""
    return (
        '<section class="mineru-workspace-intro">'
        '<div class="mineru-workspace-title-group">'
        f'<div class="mineru-workspace-eyebrow">{render_client_i18n_text(i18n, "current_tool")}</div>'
        f'<h1>{render_client_i18n_text(i18n, "header_title")}</h1>'
        f'<p>{render_client_i18n_text(i18n, "header_subtitle")}</p>'
        '</div>'
        '<div class="mineru-format-list" aria-label="Supported formats">'
        '<span>PDF</span><span>DOCX</span><span>PPTX</span><span>XLSX</span>'
        '</div>'
        '</section>'
    )

all_lang = list(PUBLIC_OCR_LANGUAGE_CHOICES)


def safe_stem(file_path):
    stem = Path(file_path).stem
    # 只保留字母、数字、下划线和点，其他字符替换为下划线
    return re.sub(r'[^\w.]', '_', stem)


def to_pdf(file_path):

    if file_path is None:
        return None

    pdf_bytes = read_fn(file_path)

    # unique_filename = f'{uuid.uuid4()}.pdf'
    unique_filename = f'{safe_stem(file_path)}.pdf'

    # 构建完整的文件路径
    tmp_file_path = os.path.join(os.path.dirname(file_path), unique_filename)

    # 将字节数据写入文件
    with open(tmp_file_path, 'wb') as tmp_pdf_file:
        tmp_pdf_file.write(pdf_bytes)

    return tmp_file_path


def to_pdf_preview(file_path):
    """用于 PDF 预览的转换函数，office 文件不支持预览，返回 None。"""
    if file_path is None:
        return None
    file_suffix = Path(file_path).suffix.lower().lstrip('.')
    if file_suffix in office_suffixes:
        return None
    return to_pdf(file_path)


def build_gradio_file_public_url(file_path, request: gr.Request):
    """根据当前 Gradio 请求构建上传文件的外部访问 URL，兼容本地和反代环境。"""
    headers = getattr(request, "headers", None) or {}
    host = (
        headers.get('x-forwarded-host')
        or headers.get('host', 'localhost:7860')
    )
    proto = headers.get('x-forwarded-proto', 'http')
    return f"{proto}://{host}/gradio_api/file={quote(str(file_path), safe='/:')}"


def build_short_gradio_file_url(public_url, file_path):
    """生成页面展示用的短链接文本，保留站点前缀和文件名尾部以避免长路径撑破预览区。"""
    base_url = public_url.split("/gradio_api/file=", 1)[0]
    file_name = Path(file_path).name
    file_suffix = Path(file_name).suffix
    file_stem = Path(file_name).stem
    short_file_name = f"{file_stem[-12:]}{file_suffix}" if file_stem else file_name
    return f"{base_url}/....{short_file_name}"


def build_office_preview_html(file_path, request: gr.Request, i18n=None):
    """生成 Office 在线预览 HTML，并提示该预览依赖外部 Microsoft 服务访问。"""
    public_url = build_gradio_file_public_url(file_path, request)
    short_public_url = build_short_gradio_file_url(public_url, file_path)
    viewer_url = (
        "https://view.officeapps.live.com/op/embed.aspx?src="
        f"{quote(public_url, safe='')}"
    )
    title = render_client_i18n_text(i18n, "office_preview_title")
    notice = render_client_i18n_text(i18n, "office_preview_notice")
    source_link = render_client_i18n_text(i18n, "office_preview_source_link")
    ignore_once = render_client_i18n_text(i18n, "office_preview_ignore_once")
    ignore_forever = render_client_i18n_text(i18n, "office_preview_ignore_forever")
    return (
        '<div class="office-preview-shell">'
        '<div class="office-preview-notice">'
        '<div class="office-preview-copy">'
        f"<strong>{title}</strong>"
        f"<span>{notice}</span>"
        '<div class="office-preview-source-link">'
        f"{source_link}: "
        f"{html_lib.escape(short_public_url)}"
        "</div>"
        "</div>"
        '<div class="office-preview-actions">'
        f'<button type="button" class="office-preview-ignore-once">{ignore_once}</button>'
        f'<button type="button" class="office-preview-ignore-forever">{ignore_forever}</button>'
        "</div>"
        "</div>"
        f'<iframe class="office-preview-frame" src="{html_lib.escape(viewer_url)}" '
        'frameborder="0"></iframe>'
        "</div>"
    )


def update_file_options_html(file_path, request: gr.Request, i18n=None):
    """处理文件上传第一阶段：根据文件类型更新 options_group 和 office_html。
    将 doc_show（gradio_pdf.PDF）的更新拆分到独立的 .then() 事件中，
    以规避 gradio_pdf 0.0.24 在 Gradio 6 中对 value=None 处理不当导致的
    整个事件 processing 状态卡死的兼容性问题。
    """
    if file_path is None:
        return (
            gr.update(visible=True),             # options_group - 恢复显示
            gr.update(value="", visible=False),  # office_html - 隐藏
        )

    file_suffix = Path(file_path).suffix.lower().lstrip('.')
    is_office = file_suffix in office_suffixes

    if is_office:
        html_content = build_office_preview_html(file_path, request, i18n)
        return (
            gr.update(visible=False),                    # options_group - 隐藏
            gr.update(value=html_content, visible=True), # office_html - 显示
        )
    else:
        return (
            gr.update(visible=True),             # options_group - 显示
            gr.update(value="", visible=False),  # office_html - 隐藏
        )


def update_doc_show(file_path):
    """处理文件上传第二阶段：单独更新 doc_show（gradio_pdf.PDF）组件。
    对 office 文件仅改变 visible，避免传递 value=None 触发
    gradio_pdf 0.0.24 在 Gradio 6 中无法完成的加载周期。
    """
    if file_path is None:
        # 无文件时恢复显示并清空（clear 按钮路径）
        return gr.update(value=None, visible=True)

    file_suffix = Path(file_path).suffix.lower().lstrip('.')
    is_office = file_suffix in office_suffixes

    if is_office:
        # 仅隐藏，不改变 value，避免触发 gradio_pdf 加载周期导致事件 pending 卡死
        return gr.update(visible=False)
    else:
        pdf_path = to_pdf_preview(file_path)
        return gr.update(value=pdf_path, visible=True)


@click.command(context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.pass_context
@click.option(
    '--enable-example',
    'example_enable',
    type=bool,
    help="Включить примеры файлов для загрузки. "
         "Файлы примеров нужно поместить в папку `examples` внутри директории, из которой запускается команда.",
    default=True,
)
@click.option(
    '--enable-http-client',
    'http_client_enable',
    type=bool,
    help="Включить бэкенд http-client для подключения к OpenAI-совместимым серверам.",
    default=False,
)
@click.option(
    '--enable-api',
    'api_enable',
    type=bool,
    help="Включить Gradio API для обслуживания приложения.",
    default=True,
)
@click.option(
    '--max-convert-pages',
    'max_convert_pages',
    type=int,
    help="Задать максимальное число страниц для конвертации из PDF в Markdown.",
    default=1000,
)
@click.option(
    '--server-name',
    'server_name',
    type=str,
    help="Задать имя сервера для приложения Gradio.",
    default=None,
)
@click.option(
    '--server-port',
    'server_port',
    type=int,
    help="Задать порт сервера для приложения Gradio.",
    default=None,
)
@click.option(
    '--api-url',
    'api_url',
    type=str,
    help="Базовый URL MinerU FastAPI. Если не указан, gradio запускает переиспользуемый локальный сервис mineru-api.",
    default=None,
)
@click.option(
    '--enable-vlm-preload',
    'enable_vlm_preload',
    type=bool,
    help="Предзагружать локальную VLM-модель при запуске gradio с локальным сервисом mineru-api.",
    default=False,
)
@click.option(
    '--client-side-output-generation',
    'client_side_output_generation',
    type=bool,
    help="Формировать Markdown и списки содержимого локально на основе middle json, полученного от сервера.",
    default=False,
)
@click.option(
    '--latex-delimiters-type',
    'latex_delimiters_type',
    type=click.Choice(['a', 'b', 'all']),
    help="Задать тип разделителей LaTeX для рендеринга Markdown: "
         "'a' — тип '$', 'b' — тип '()[]', 'all' — оба типа.",
    default='all',
)
def main(ctx,
        example_enable,
        http_client_enable,
        api_enable, max_convert_pages,
        server_name, server_port, api_url, enable_vlm_preload,
        client_side_output_generation, latex_delimiters_type, **kwargs
):

    # 创建 i18n 实例，支持中英文
    i18n = gr.I18n(
        en={
            "upload_file": "Select or paste a file to upload\nPDF, image, DOCX, PPTX, or XLSX",
            "batch_title": "Batch recognition",
            "batch_files": "Drop several files (results will be available as a ZIP)",
            "batch_source_folder": "Source folder on local drive D:",
            "batch_source_folder_info": "For saving beside originals, enter a folder on D:; files are processed directly from it.",
            "batch_recursive": "Include subfolders",
            "batch_save_next_to_source": "Save each result next to its original",
            "batch_convert": "Process batch",
            "batch_result": "Batch result ZIP",
            "batch_report": "Batch report",
            "batch_processing": "Processing",
            "batch_progress": "Batch progress",
            "batch_completed": "Batch completed",
            "batch_failed": "Failed",
            "batch_no_files": "No supported documents were found",
            "batch_error": "Batch setup error",
            "batch_archive_error": "Could not create the result archive",
            "app_workspace": "Workspace",
            "tools_label": "Tools",
            "active_tool": "Active tool",
            "future_tools": "Future tools",
            "tool_slot": "Available slot",
            "tool_slot_hint": "For a new tool",
            "local_mode": "Local mode",
            "language_label": "Language",
            "current_tool": "Current tool",
            "header_title": "Document recognition",
            "header_subtitle": "Convert PDF, Office documents, and images into structured Markdown and JSON.",
            "header_support_text": "If you found our project helpful, please give us a ⭐️ to support us!",
            "header_stars_alt": "stars",
            "header_code_link": "Code",
            "header_model_link": "Model",
            "header_model_huggingface_link": "Hugging Face",
            "header_model_modelscope_link": "ModelScope",
            "header_paper_link": "Paper",
            "header_paper_mineru_report": "MinerU · arXiv: 2409.18839",
            "header_paper_mineru25_report": "MinerU 2.5 · arXiv: 2509.22186",
            "header_paper_mineru25pro_report": "MinerU 2.5 Pro · arXiv: 2604.04771",
            "header_homepage_link": "Homepage",
            "header_download_link": "Download",
            "max_pages": "Max convert pages",
            "backend": "Backend",
            "backend_label_hybrid": "Hybrid (Recommended)",
            "backend_label_pipeline": "Pipeline (Stable multilingual)",
            "backend_label_vlm": "VLM (High-precision Chinese/English)",
            "backend_label_remote_vlm": "Remote VLM",
            "backend_label_remote_hybrid": "Remote Hybrid",
            "server_url": "Server URL",
            "server_url_info": "OpenAI-compatible server URL for http-client backend.",
            "recognition_options": "**Recognition Options:**",
            "advanced_options": "Advanced options",
            "table_enable": "Enable table recognition",
            "table_info": "If disabled, tables will be shown as images.",
            "image_analysis_enable": "Enable image analysis",
            "image_analysis_info": "If disabled, image/chart blocks will keep layout positions but skip VLM image/chart analysis.",
            "formula_label_vlm": "Enable display formula recognition",
            "formula_label_pipeline": "Enable formula recognition",
            "formula_label_hybrid": "Enable inline formula recognition",
            "formula_info_vlm": "If disabled, display formulas will be shown as images.",
            "formula_info_pipeline": "If disabled, display formulas will be shown as images, and inline formulas will not be detected or parsed.",
            "formula_info_hybrid": "If disabled, inline formulas will not be detected or parsed.",
            "ocr_language": "OCR Language",
            "ocr_language_info": "Select the OCR language for image-based PDFs and images.",
            "force_ocr": "Force enable OCR",
            "force_ocr_info": "Enable only if the result is extremely poor. Requires correct OCR language.",
            "force_ocr_info_hybrid": "Enable only if the result is extremely poor.",
            "convert": "Convert",
            "clear": "Clear",
            "doc_preview": "Document preview",
            "document_tab_title": "Document",
            "examples": "Examples:",
            "convert_status": "Conversion Status",
            "convert_result": "Convert result",
            "result_file": "Result file",
            "download_docx": "Download DOCX",
            "download_searchable_pdf": "PDF with text layer",
            "docx_preview": "DOCX preview",
            "md_rendering": "Markdown rendering",
            "md_text": "Markdown text",
            "content_list_json": "JSON Content List",
            "status_idle_title": "Waiting",
            "status_idle_hint": "Upload a file and start conversion.",
            "status_latest": "Latest status",
            "status_step_prepare": "Prepare",
            "status_step_check": "Check service",
            "status_step_submit": "Submit",
            "status_step_queue": "Queue",
            "status_step_process": "Parse",
            "status_step_download": "Download",
            "status_step_outputs": "Build outputs",
            "status_step_done": "Done",
            "status_step_failed": "Failed",
            "office_preview_title": "Office online preview",
            "office_preview_notice": "This preview requires the current file to be reachable by Microsoft Office Online. Conversion does not depend on this preview.",
            "office_preview_source_link": "File url",
            "office_preview_ignore_once": "Dismiss",
            "office_preview_ignore_forever": "Always dismiss",
            "backend_info_vlm": "Multimodal large-model end-to-end parsing, high accuracy.",
            "backend_info_pipeline": "Traditional multi-model pipeline parsing, low resource usage, hallucination-free.",
            "backend_info_hybrid": "Exclusive hybrid engine parsing, ultra-high accuracy.",
            "backend_info_default": "Select the backend engine for document parsing.",
            "hybrid_effort": "Hybrid effort",
            "hybrid_effort_info": "Medium is faster. High is more accurate and may take longer.",
            "save_button": "💾 Save",
            "edit_markdown_rendering": "✏️ Edit Markdown",
            "close_markdown_editor": "← Back to rendering",
            "save_markdown_rendering": "💾 Save rendering edits",
            "save_error_no_result": "No conversion result to save yet. Convert a document first.",
            "save_md_success": "Markdown text saved",
            "save_md_error": "Failed to save Markdown text",
            "save_json_success": "JSON content list saved",
            "save_json_invalid": "Invalid JSON, nothing was saved",
            "save_json_error": "Failed to save JSON content list",
            "visual_editor_tab": "Visual editor",
            "editor_page_word": "Page",
            "editor_no_result": "No conversion result yet. Convert a document first.",
            "editor_no_origin_pdf": "The visual editor is only available for PDF/scanned documents.",
            "editor_load_error": "Failed to load the editor page",
            "editor_block_no_text": "This block has no editable text",
            "editor_no_block_at_click": "No recognized block at the clicked point",
            "editor_block_selected": "Block selected. Edit its content, then apply the edit.",
            "editor_save_error": "Failed to save the edit",
            "editor_save_success": "Edit saved",
            "editor_table_structure_error": "Table structure changed, edit was not saved",
            "editor_refresh_error": "Failed to refresh DOCX and PDF",
            "editor_refresh_success": "DOCX and PDF refreshed",
            "editor_apply_edit": "Apply edit",
            "editor_deselect": "Deselect",
            "editor_block_panel_title": "Block editing",
            "editor_click_hint": "Click a coloured text block — its recognised text will open on the right for editing.",
            "editor_refresh_exports": "Refresh DOCX and PDF",
            "editor_exports_dirty": "There are edits not yet reflected in the exports",
            "editor_zoom_fit": "Fit",
            "editor_no_suspects": "No low-confidence blocks found.",
            "editor_suspect_unavailable": "This engine did not provide confidence scores for this document.",
            "editor_next_suspect": "→ Next low-confidence block",
            "editor_thumbnails_title": "Pages",
            "editor_select_trigger_label": "Select document block",
            "editor_table_picker": "Detected tables",
            "editor_table_word": "table",
        },
        zh={
            "upload_file": "请选择或粘贴要上传的文件\nPDF、图片、DOCX、PPTX 或 XLSX",
            "batch_title": "批量识别",
            "batch_files": "拖入多个文件（结果将提供为 ZIP）",
            "batch_source_folder": "本地 D: 盘中的源文件夹",
            "batch_source_folder_info": "要保存到原文件旁，请输入 D: 中的文件夹；系统将直接处理其中的文件。",
            "batch_recursive": "包含子文件夹",
            "batch_save_next_to_source": "将每个结果保存到原文件旁",
            "batch_convert": "处理批次",
            "batch_result": "批量结果 ZIP",
            "batch_report": "批量报告",
            "batch_processing": "正在处理",
            "batch_progress": "批量进度",
            "batch_completed": "批量完成",
            "batch_failed": "失败",
            "batch_no_files": "未找到支持的文档",
            "batch_error": "批量设置错误",
            "batch_archive_error": "无法创建结果压缩包",
            "app_workspace": "工作区",
            "tools_label": "工具",
            "active_tool": "当前工具",
            "future_tools": "后续工具",
            "tool_slot": "空闲位置",
            "tool_slot_hint": "用于新工具",
            "local_mode": "本地模式",
            "language_label": "语言",
            "current_tool": "当前工具",
            "header_title": "文档识别",
            "header_subtitle": "将 PDF、Office 文档和图片转换为结构化 Markdown 与 JSON。",
            "header_support_text": "如果我们的项目对你有帮助，请点亮 ⭐️ 支持我们！",
            "header_stars_alt": "GitHub 星标",
            "header_code_link": "代码",
            "header_model_link": "模型",
            "header_model_huggingface_link": "Hugging Face",
            "header_model_modelscope_link": "ModelScope",
            "header_paper_link": "论文",
            "header_paper_mineru_report": "MinerU · arXiv: 2409.18839",
            "header_paper_mineru25_report": "MinerU 2.5 · arXiv: 2509.22186",
            "header_paper_mineru25pro_report": "MinerU 2.5 Pro · arXiv: 2604.04771",
            "header_homepage_link": "主页",
            "header_download_link": "下载",
            "max_pages": "最大转换页数",
            "backend": "解析后端",
            "backend_label_hybrid": "Hybrid 推荐",
            "backend_label_pipeline": "Pipeline 稳定多语言",
            "backend_label_vlm": "VLM 高精度中英文",
            "backend_label_remote_vlm": "Remote VLM",
            "backend_label_remote_hybrid": "Remote Hybrid",
            "server_url": "服务器地址",
            "server_url_info": "http-client 后端的 OpenAI 兼容服务器地址。",
            "recognition_options": "**识别选项：**",
            "advanced_options": "高级选项",
            "table_enable": "启用表格识别",
            "table_info": "禁用后，表格将显示为图片。",
            "image_analysis_enable": "启用图片分析",
            "image_analysis_info": "禁用后，图片/图表块仍保留版面位置，但跳过 VLM 图片/图表分析。",
            "formula_label_vlm": "启用行间公式识别",
            "formula_label_pipeline": "启用公式识别",
            "formula_label_hybrid": "启用行内公式识别",
            "formula_info_vlm": "禁用后，行间公式将显示为图片。",
            "formula_info_pipeline": "禁用后，行间公式将显示为图片，行内公式将不会被检测或解析。",
            "formula_info_hybrid": "禁用后，行内公式将不会被检测或解析。",
            "ocr_language": "OCR 语言",
            "ocr_language_info": "为扫描版 PDF 和图片选择 OCR 语言。",
            "force_ocr": "强制启用 OCR",
            "force_ocr_info": "仅在识别效果极差时启用，需选择正确的 OCR 语言。",
            "force_ocr_info_hybrid": "仅在识别效果极差时启用。",
            "convert": "转换",
            "clear": "清除",
            "doc_preview": "文档预览",
            "document_tab_title": "文档",
            "examples": "示例：",
            "convert_status": "转换状态",
            "convert_result": "转换结果",
            "result_file": "结果文件",
            "download_docx": "下载 DOCX",
            "download_searchable_pdf": "带文字层的 PDF",
            "docx_preview": "DOCX 预览",
            "md_rendering": "Markdown 渲染",
            "md_text": "Markdown 文本",
            "content_list_json": "JSON 内容列表",
            "status_idle_title": "等待任务",
            "status_idle_hint": "上传文件后开始转换。",
            "status_latest": "最新状态",
            "status_step_prepare": "准备请求",
            "status_step_check": "检查服务",
            "status_step_submit": "提交任务",
            "status_step_queue": "排队",
            "status_step_process": "解析中",
            "status_step_download": "下载结果",
            "status_step_outputs": "整理输出",
            "status_step_done": "完成",
            "status_step_failed": "失败",
            "office_preview_title": "Office 在线预览",
            "office_preview_notice": "该预览需要当前文件可被 Microsoft 在线预览服务访问，转换不依赖该预览。",
            "office_preview_source_link": "文件链接",
            "office_preview_ignore_once": "忽略",
            "office_preview_ignore_forever": "不再提示",
            "backend_info_vlm": "多模态大模型端到端解析，高精度",
            "backend_info_pipeline": "传统多模型管道解析，低资源，无幻觉",
            "backend_info_hybrid": "独家混合引擎解析，超高精度",
            "backend_info_default": "选择文档解析的后端引擎。",
            "hybrid_effort": "解析强度",
            "hybrid_effort_info": "Medium 速度更快；High 精度更高，耗时可能更长。",
            "save_button": "💾 保存",
            "edit_markdown_rendering": "✏️ 编辑 Markdown",
            "close_markdown_editor": "← 返回渲染预览",
            "save_markdown_rendering": "💾 保存渲染编辑",
            "save_error_no_result": "尚无可保存的转换结果，请先完成转换。",
            "save_md_success": "Markdown 文本已保存",
            "save_md_error": "保存 Markdown 文本失败",
            "save_json_success": "JSON 内容列表已保存",
            "save_json_invalid": "JSON 格式无效，未保存",
            "save_json_error": "保存 JSON 内容列表失败",
            "visual_editor_tab": "可视化编辑器",
            "editor_page_word": "页",
            "editor_no_result": "暂无转换结果，请先完成转换。",
            "editor_no_origin_pdf": "可视化编辑器仅支持 PDF/扫描件。",
            "editor_load_error": "加载编辑器页面失败",
            "editor_block_no_text": "该区块没有可编辑文本",
            "editor_no_block_at_click": "点击位置没有已识别的区块",
            "editor_block_selected": "已选中区块。编辑内容后，应用修改。",
            "editor_save_error": "保存修改失败",
            "editor_save_success": "修改已保存",
            "editor_table_structure_error": "表格结构已更改，未保存修改",
            "editor_refresh_error": "刷新 DOCX 和 PDF 失败",
            "editor_refresh_success": "DOCX 和 PDF 已刷新",
            "editor_apply_edit": "应用修改",
            "editor_deselect": "取消选择",
            "editor_block_panel_title": "编辑所选块",
            "editor_click_hint": "点击彩色文本块：右侧将打开识别出的文字，可直接编辑。",
            "editor_refresh_exports": "刷新 DOCX 和 PDF",
            "editor_exports_dirty": "存在尚未反映到导出文件中的修改",
            "editor_zoom_fit": "适应页面",
            "editor_no_suspects": "未找到低置信度区块。",
            "editor_suspect_unavailable": "此引擎未为该文档提供置信度评分。",
            "editor_next_suspect": "→ 下一个低置信度区块",
            "editor_thumbnails_title": "页面",
            "editor_select_trigger_label": "选择文档区块",
            "editor_table_picker": "已识别表格",
            "editor_table_word": "表格",
        },
        ru={
            "upload_file": "Выберите или вставьте файл для загрузки\nPDF, изображение, DOCX, PPTX или XLSX",
            "batch_title": "Пакетное распознавание",
            "batch_files": "Перетащите несколько файлов (результат будет доступен одним ZIP)",
            "batch_source_folder": "Исходная папка на локальном диске D:",
            "batch_source_folder_info": "Чтобы сохранить рядом с оригиналами, укажите папку на D: — файлы будут обработаны прямо из неё.",
            "batch_recursive": "Включая вложенные папки",
            "batch_save_next_to_source": "Сохранять каждый результат рядом с оригиналом",
            "batch_convert": "Обработать пакет",
            "batch_result": "ZIP с результатами пакета",
            "batch_report": "Отчёт по пакету",
            "batch_processing": "Обработка",
            "batch_progress": "Прогресс пакета",
            "batch_completed": "Пакет завершён",
            "batch_failed": "Ошибок",
            "batch_no_files": "Поддерживаемые документы не найдены",
            "batch_error": "Ошибка подготовки пакета",
            "batch_archive_error": "Не удалось создать архив результатов",
            "app_workspace": "Рабочая среда",
            "tools_label": "Инструменты",
            "active_tool": "Активный инструмент",
            "future_tools": "Будущие инструменты",
            "tool_slot": "Свободный слот",
            "tool_slot_hint": "Для нового инструмента",
            "local_mode": "Локальный режим",
            "language_label": "Язык",
            "current_tool": "Текущий инструмент",
            "header_title": "Распознавание документов",
            "header_subtitle": "Преобразование PDF, документов Office и изображений в структурированные Markdown и JSON.",
            "header_support_text": "Если наш проект оказался полезен, поставьте ⭐️ — это нас поддержит!",
            "header_stars_alt": "звёзды",
            "header_code_link": "Код",
            "header_model_link": "Модель",
            "header_model_huggingface_link": "Hugging Face",
            "header_model_modelscope_link": "ModelScope",
            "header_paper_link": "Статья",
            "header_paper_mineru_report": "MinerU · arXiv: 2409.18839",
            "header_paper_mineru25_report": "MinerU 2.5 · arXiv: 2509.22186",
            "header_paper_mineru25pro_report": "MinerU 2.5 Pro · arXiv: 2604.04771",
            "header_homepage_link": "Главная",
            "header_download_link": "Скачать",
            "max_pages": "Макс. число страниц для конвертации",
            "backend": "Бэкенд",
            "backend_label_hybrid": "Hybrid (рекомендуется)",
            "backend_label_pipeline": "Pipeline (стабильный, многоязычный)",
            "backend_label_vlm": "VLM (высокая точность для китайского/английского)",
            "backend_label_remote_vlm": "Remote VLM",
            "backend_label_remote_hybrid": "Remote Hybrid",
            "server_url": "Адрес сервера",
            "server_url_info": "URL OpenAI-совместимого сервера для бэкенда http-client.",
            "recognition_options": "**Параметры распознавания:**",
            "advanced_options": "Дополнительные параметры",
            "table_enable": "Распознавать таблицы",
            "table_info": "Если отключено, таблицы будут показаны как изображения.",
            "image_analysis_enable": "Анализировать изображения",
            "image_analysis_info": "Если отключено, блоки изображений/диаграмм сохранят своё положение в макете, но VLM-анализ изображений/диаграмм выполняться не будет.",
            "formula_label_vlm": "Распознавать блочные формулы",
            "formula_label_pipeline": "Распознавать формулы",
            "formula_label_hybrid": "Распознавать строчные формулы",
            "formula_info_vlm": "Если отключено, блочные формулы будут показаны как изображения.",
            "formula_info_pipeline": "Если отключено, блочные формулы будут показаны как изображения, а строчные формулы не будут обнаруживаться и распознаваться.",
            "formula_info_hybrid": "Если отключено, строчные формулы не будут обнаруживаться и распознаваться.",
            "ocr_language": "Язык OCR",
            "ocr_language_info": "Выберите язык OCR для сканированных PDF и изображений.",
            "force_ocr": "Принудительно включить OCR",
            "force_ocr_info": "Включайте, только если результат крайне низкого качества. Требуется правильно указанный язык OCR.",
            "force_ocr_info_hybrid": "Включайте, только если результат крайне низкого качества.",
            "convert": "Конвертировать",
            "clear": "Очистить",
            "doc_preview": "Предпросмотр документа",
            "document_tab_title": "Документ",
            "examples": "Примеры:",
            "convert_status": "Статус конвертации",
            "convert_result": "Результат конвертации",
            "result_file": "Файл результата",
            "download_docx": "Скачать DOCX",
            "download_searchable_pdf": "PDF (текстовый слой)",
            "docx_preview": "Превью DOCX",
            "md_rendering": "Рендеринг Markdown",
            "md_text": "Текст Markdown",
            "content_list_json": "Список содержимого JSON",
            "status_idle_title": "Ожидание",
            "status_idle_hint": "Загрузите файл и начните конвертацию.",
            "status_latest": "Текущий статус",
            "status_step_prepare": "Подготовка",
            "status_step_check": "Проверка сервиса",
            "status_step_submit": "Отправка",
            "status_step_queue": "Очередь",
            "status_step_process": "Разбор",
            "status_step_download": "Загрузка",
            "status_step_outputs": "Формирование результатов",
            "status_step_done": "Готово",
            "status_step_failed": "Ошибка",
            "office_preview_title": "Онлайн-предпросмотр Office",
            "office_preview_notice": "Для этого предпросмотра текущий файл должен быть доступен сервису Microsoft Office Online. Конвертация от этого предпросмотра не зависит.",
            "office_preview_source_link": "Ссылка на файл",
            "office_preview_ignore_once": "Скрыть",
            "office_preview_ignore_forever": "Больше не показывать",
            "backend_info_vlm": "Сквозной разбор мультимодальной большой моделью, высокая точность.",
            "backend_info_pipeline": "Классический многомодельный конвейер разбора, малое потребление ресурсов, без галлюцинаций.",
            "backend_info_hybrid": "Разбор эксклюзивным гибридным движком, сверхвысокая точность.",
            "backend_info_default": "Выберите бэкенд для разбора документа.",
            "hybrid_effort": "Интенсивность разбора",
            "hybrid_effort_info": "Medium — быстрее. High — точнее, но может занять больше времени.",
            "save_button": "💾 Сохранить",
            "edit_markdown_rendering": "✏️ Редактировать Markdown",
            "close_markdown_editor": "← К рендерингу",
            "save_markdown_rendering": "💾 Сохранить правки",
            "save_error_no_result": "Нет результата конвертации для сохранения. Сначала выполните конвертацию.",
            "save_md_success": "Текст Markdown сохранён",
            "save_md_error": "Не удалось сохранить текст Markdown",
            "save_json_success": "Список содержимого JSON сохранён",
            "save_json_invalid": "Некорректный JSON, сохранение не выполнено",
            "save_json_error": "Не удалось сохранить список содержимого JSON",
            "visual_editor_tab": "Визуальный редактор",
            "editor_page_word": "Страница",
            "editor_no_result": "Нет результата конвертации. Сначала выполните конвертацию.",
            "editor_no_origin_pdf": "Визуальный редактор доступен только для PDF и сканов.",
            "editor_load_error": "Не удалось загрузить страницу редактора",
            "editor_block_no_text": "У этого блока нет редактируемого текста",
            "editor_no_block_at_click": "В точке клика нет распознанного блока",
            "editor_block_selected": "Блок выбран. Отредактируйте содержимое и примените правку.",
            "editor_save_error": "Не удалось сохранить правку",
            "editor_save_success": "Правка сохранена",
            "editor_table_structure_error": "Структура таблицы изменена, правка не сохранена",
            "editor_refresh_error": "Не удалось обновить DOCX и PDF",
            "editor_refresh_success": "DOCX и PDF обновлены",
            "editor_apply_edit": "Применить правку",
            "editor_deselect": "Отменить выбор",
            "editor_block_panel_title": "Редактирование блока",
            "editor_click_hint": "Щёлкните по цветной рамке текста — справа сразу откроется поле с распознанным текстом.",
            "editor_refresh_exports": "Обновить DOCX и PDF",
            "editor_exports_dirty": "Есть правки, не попавшие в экспорт",
            "editor_zoom_fit": "Вписать",
            "editor_no_suspects": "Блоков с низкой уверенностью не найдено.",
            "editor_suspect_unavailable": "Этот движок не передал оценки уверенности для документа.",
            "editor_next_suspect": "→ К следующему неуверенному блоку",
            "editor_thumbnails_title": "Страницы",
            "editor_select_trigger_label": "Выбрать блок документа",
            "editor_table_picker": "Распознанные таблицы",
            "editor_table_word": "таблица",
        },
        uk={
            "upload_file": "Виберіть або вставте файл для завантаження\nPDF, зображення, DOCX, PPTX або XLSX",
            "batch_title": "Пакетне розпізнавання",
            "batch_files": "Перетягніть кілька файлів (результат буде доступний одним ZIP)",
            "batch_source_folder": "Вихідна папка на локальному диску D:",
            "batch_source_folder_info": "Щоб зберегти поруч з оригіналами, вкажіть папку на D: — файли буде оброблено безпосередньо з неї.",
            "batch_recursive": "З вкладеними папками",
            "batch_save_next_to_source": "Зберігати кожен результат поруч з оригіналом",
            "batch_convert": "Обробити пакет",
            "batch_result": "ZIP з результатами пакета",
            "batch_report": "Звіт за пакетом",
            "batch_processing": "Обробка",
            "batch_progress": "Перебіг пакета",
            "batch_completed": "Пакет завершено",
            "batch_failed": "Помилок",
            "batch_no_files": "Підтримуваних документів не знайдено",
            "batch_error": "Помилка підготовки пакета",
            "batch_archive_error": "Не вдалося створити архів результатів",
            "app_workspace": "Робоче середовище",
            "tools_label": "Інструменти",
            "active_tool": "Активний інструмент",
            "future_tools": "Майбутні інструменти",
            "tool_slot": "Вільний слот",
            "tool_slot_hint": "Для нового інструмента",
            "local_mode": "Локальний режим",
            "language_label": "Мова",
            "current_tool": "Поточний інструмент",
            "header_title": "Розпізнавання документів",
            "header_subtitle": "Перетворення PDF, документів Office і зображень на структуровані Markdown та JSON.",
            "header_support_text": "Якщо наш проєкт став вам у пригоді, поставте ⭐️ — це нас підтримає!",
            "header_stars_alt": "зірки",
            "header_code_link": "Код",
            "header_model_link": "Модель",
            "header_model_huggingface_link": "Hugging Face",
            "header_model_modelscope_link": "ModelScope",
            "header_paper_link": "Стаття",
            "header_paper_mineru_report": "MinerU · arXiv: 2409.18839",
            "header_paper_mineru25_report": "MinerU 2.5 · arXiv: 2509.22186",
            "header_paper_mineru25pro_report": "MinerU 2.5 Pro · arXiv: 2604.04771",
            "header_homepage_link": "Головна",
            "header_download_link": "Завантажити",
            "max_pages": "Макс. кількість сторінок для конвертації",
            "backend": "Бекенд",
            "backend_label_hybrid": "Hybrid (рекомендовано)",
            "backend_label_pipeline": "Pipeline (стабільний, багатомовний)",
            "backend_label_vlm": "VLM (висока точність для китайської/англійської)",
            "backend_label_remote_vlm": "Remote VLM",
            "backend_label_remote_hybrid": "Remote Hybrid",
            "server_url": "Адреса сервера",
            "server_url_info": "URL OpenAI-сумісного сервера для бекенда http-client.",
            "recognition_options": "**Параметри розпізнавання:**",
            "advanced_options": "Додаткові параметри",
            "table_enable": "Розпізнавати таблиці",
            "table_info": "Якщо вимкнено, таблиці відображатимуться як зображення.",
            "image_analysis_enable": "Аналізувати зображення",
            "image_analysis_info": "Якщо вимкнено, блоки зображень і діаграм збережуть своє положення в макеті, але VLM-аналіз не виконуватиметься.",
            "formula_label_vlm": "Розпізнавати блокові формули",
            "formula_label_pipeline": "Розпізнавати формули",
            "formula_label_hybrid": "Розпізнавати рядкові формули",
            "formula_info_vlm": "Якщо вимкнено, блокові формули відображатимуться як зображення.",
            "formula_info_pipeline": "Якщо вимкнено, блокові формули відображатимуться як зображення, а рядкові формули не виявлятимуться й не розпізнаватимуться.",
            "formula_info_hybrid": "Якщо вимкнено, рядкові формули не виявлятимуться й не розпізнаватимуться.",
            "ocr_language": "Мова OCR",
            "ocr_language_info": "Виберіть мову OCR для сканованих PDF і зображень.",
            "force_ocr": "Примусово ввімкнути OCR",
            "force_ocr_info": "Вмикайте лише за дуже низької якості результату. Потрібно правильно вибрати мову OCR.",
            "force_ocr_info_hybrid": "Вмикайте лише за дуже низької якості результату.",
            "convert": "Конвертувати",
            "clear": "Очистити",
            "doc_preview": "Попередній перегляд документа",
            "document_tab_title": "Документ",
            "examples": "Приклади:",
            "convert_status": "Стан конвертації",
            "convert_result": "Результат конвертації",
            "result_file": "Файл результату",
            "download_docx": "Завантажити DOCX",
            "download_searchable_pdf": "PDF (текстовий шар)",
            "docx_preview": "Попередній перегляд DOCX",
            "md_rendering": "Відтворення Markdown",
            "md_text": "Текст Markdown",
            "content_list_json": "Список вмісту JSON",
            "status_idle_title": "Очікування",
            "status_idle_hint": "Завантажте файл і почніть конвертацію.",
            "status_latest": "Поточний стан",
            "status_step_prepare": "Підготовка",
            "status_step_check": "Перевірка сервісу",
            "status_step_submit": "Надсилання",
            "status_step_queue": "Черга",
            "status_step_process": "Розпізнавання",
            "status_step_download": "Завантаження",
            "status_step_outputs": "Формування результатів",
            "status_step_done": "Готово",
            "status_step_failed": "Помилка",
            "office_preview_title": "Онлайн-перегляд Office",
            "office_preview_notice": "Для цього перегляду поточний файл має бути доступним сервісу Microsoft Office Online. Конвертація від цього перегляду не залежить.",
            "office_preview_source_link": "Посилання на файл",
            "office_preview_ignore_once": "Приховати",
            "office_preview_ignore_forever": "Більше не показувати",
            "backend_info_vlm": "Наскрізне розпізнавання мультимодальною великою моделлю з високою точністю.",
            "backend_info_pipeline": "Класичний багатомодельний конвеєр із низьким споживанням ресурсів і без галюцинацій.",
            "backend_info_hybrid": "Розпізнавання ексклюзивним гібридним рушієм із надвисокою точністю.",
            "backend_info_default": "Виберіть бекенд для розпізнавання документа.",
            "hybrid_effort": "Інтенсивність розпізнавання",
            "hybrid_effort_info": "Medium — швидше. High — точніше, але може тривати довше.",
            "upload_text.drop_file": "Перетягніть файл сюди",
            "upload_text.click_to_upload": "Натисніть для завантаження",
            "upload_text.paste_clipboard": "Вставити з буфера обміну",
            "common.or": "або",
            "common.clear": "Очистити",
            "common.settings": "Налаштування",
            "common.logo": "Логотип",
            "common.built_with_gradio": "Створено за допомогою Gradio",
            "errors.use_via_api": "Використовувати через API",
            "errors.use_via_api_or_mcp": "Використовувати через API або MCP",
            "save_button": "💾 Зберегти",
            "edit_markdown_rendering": "✏️ Редагувати Markdown",
            "close_markdown_editor": "← До відтворення",
            "save_markdown_rendering": "💾 Зберегти правки",
            "save_error_no_result": "Немає результату конвертації для збереження. Спочатку виконайте конвертацію.",
            "save_md_success": "Текст Markdown збережено",
            "save_md_error": "Не вдалося зберегти текст Markdown",
            "save_json_success": "Список вмісту JSON збережено",
            "save_json_invalid": "Некоректний JSON, збереження не виконано",
            "save_json_error": "Не вдалося зберегти список вмісту JSON",
            "visual_editor_tab": "Візуальний редактор",
            "editor_page_word": "Сторінка",
            "editor_no_result": "Немає результату конвертації. Спочатку виконайте конвертацію.",
            "editor_no_origin_pdf": "Візуальний редактор доступний лише для PDF і сканів.",
            "editor_load_error": "Не вдалося завантажити сторінку редактора",
            "editor_block_no_text": "У цього блоку немає редагованого тексту",
            "editor_no_block_at_click": "У точці кліку немає розпізнаного блоку",
            "editor_block_selected": "Блок вибрано. Відредагуйте вміст і застосуйте правку.",
            "editor_save_error": "Не вдалося зберегти правку",
            "editor_save_success": "Правку збережено",
            "editor_table_structure_error": "Структуру таблиці змінено, правку не збережено",
            "editor_refresh_error": "Не вдалося оновити DOCX і PDF",
            "editor_refresh_success": "DOCX і PDF оновлено",
            "editor_apply_edit": "Застосувати правку",
            "editor_deselect": "Скасувати вибір",
            "editor_block_panel_title": "Редагування блоку",
            "editor_click_hint": "Клацніть кольорову рамку тексту — праворуч одразу відкриється поле з розпізнаним текстом.",
            "editor_refresh_exports": "Оновити DOCX і PDF",
            "editor_exports_dirty": "Є правки, які ще не потрапили в експорт",
            "editor_zoom_fit": "Вписати",
            "editor_no_suspects": "Блоків з низькою впевненістю не знайдено.",
            "editor_suspect_unavailable": "Цей рушій не передав оцінок упевненості для документа.",
            "editor_next_suspect": "→ До наступного невпевненого блоку",
            "editor_thumbnails_title": "Сторінки",
            "editor_select_trigger_label": "Вибрати блок документа",
            "editor_table_picker": "Розпізнані таблиці",
            "editor_table_word": "таблиця",
        },
    )

    # 根据后端类型获取公式识别标签（闭包函数以支持 i18n）
    def get_formula_label(backend_choice):
        if backend_choice.startswith("vlm"):
            return i18n("formula_label_vlm")
        elif backend_choice == "pipeline":
            return i18n("formula_label_pipeline")
        elif backend_choice.startswith("hybrid"):
            return i18n("formula_label_hybrid")
        else:
            return i18n("formula_label_pipeline")

    def get_formula_info(backend_choice):
        if backend_choice.startswith("vlm"):
            return i18n("formula_info_vlm")
        elif backend_choice == "pipeline":
            return i18n("formula_info_pipeline")
        elif backend_choice.startswith("hybrid"):
            return i18n("formula_info_hybrid")
        else:
            return ""

    def get_backend_info(backend_choice):
        return i18n(select_backend_info_key(backend_choice))

    def get_force_ocr_info(backend_choice):
        """根据后端返回强制 OCR 控件说明，避免 Hybrid 显示 pipeline 的语言提示。"""
        return i18n(select_force_ocr_info_key(backend_choice))

    def build_interface_updates(backend_choice, effort_choice):
        """构建 Gradio 后端联动更新，保证所有事件复用同一套显隐规则。"""
        formula_label_update = gr.update(label=get_formula_label(backend_choice), info=get_formula_info(backend_choice))
        backend_info_update = gr.update(info=get_backend_info(backend_choice))
        force_ocr_update = gr.update(info=get_force_ocr_info(backend_choice))

        return (
            force_ocr_update,
            formula_label_update,
            backend_info_update,
        )

    def update_interface(backend_choice, effort_choice):
        """更新可由 Gradio 稳定管理的基础界面项，易重挂载的控件交给前端状态类处理。"""
        return build_interface_updates(backend_choice, effort_choice)

    del kwargs
    _gradio_local_api_server.configure(
        resolve_gradio_local_api_cli_args(
            ctx.args,
            api_url=api_url,
            enable_vlm_preload=enable_vlm_preload,
        )
    )

    if latex_delimiters_type == 'a':
        latex_delimiters = latex_delimiters_type_a
    elif latex_delimiters_type == 'b':
        latex_delimiters = latex_delimiters_type_b
    elif latex_delimiters_type == 'all':
        latex_delimiters = latex_delimiters_type_all
    else:
        raise ValueError(f"Invalid latex delimiters type: {latex_delimiters_type}.")


    async def convert_to_markdown_stream(
        file_path,
        end_pages=10,
        is_ocr=False,
        formula_enable=True,
        table_enable=True,
        image_analysis=True,
        effort=DEFAULT_HYBRID_EFFORT,
        language="ch",
        backend="pipeline",
        url=None,
        request: gr.Request = None,
    ):
        request_locale = resolve_request_locale(request)
        async for update in stream_to_markdown(
            file_path=file_path,
            end_pages=end_pages,
            is_ocr=is_ocr,
            formula_enable=formula_enable,
            table_enable=table_enable,
            image_analysis=image_analysis,
            effort=effort,
            language=language,
            backend=backend,
            url=url,
            api_url=api_url,
            client_side_output_generation=client_side_output_generation,
        ):
            docx_path = update[6]
            # По умолчанию (промежуточные yield и отсутствие DOCX) оба блока
            # превью скрыты; value PDF-компонента не трогаем, чтобы не задеть
            # проблемный цикл загрузки gradio_pdf 0.0.24 в Gradio 6.
            docx_preview_pdf_value = gr.update(visible=False)
            docx_preview_html_value = gr.update(value="", visible=False)
            if docx_path:
                preview_pdf = await asyncio.to_thread(
                    convert_docx_to_pdf_for_preview, docx_path
                )
                if preview_pdf:
                    # Локальное превью: DOCX -> PDF через LibreOffice headless.
                    docx_preview_pdf_value = gr.update(
                        value=preview_pdf, visible=True
                    )
                else:
                    # Fallback: онлайн-просмотрщик Office (требует доступности
                    # файла для Microsoft Office Online).
                    try:
                        docx_preview_html_value = gr.update(
                            value=build_office_preview_html(docx_path, request, i18n),
                            visible=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Failed to build DOCX preview HTML for {docx_path}: {exc}"
                        )

            # PDF с текстовым слоем строится из того же results dir, что и DOCX
            # (local_md_dir/file_name восстанавливаются из docx_path, чтобы не
            # трогать внутренние yield-точки stream_to_markdown — см. паттерн
            # превью DOCX выше).
            searchable_pdf_path = None
            if docx_path:
                docx_file = Path(docx_path)
                searchable_pdf_path = await asyncio.to_thread(
                    build_gradio_searchable_pdf_result,
                    str(docx_file.parent),
                    docx_file.stem,
                )

            update = (
                render_status_steps_html(update[0], i18n, locale=request_locale),
                update[1],
                prepare_markdown_for_gradio_preview(update[2], latex_delimiters),
                update[3],
                update[4],
                update[5],
                update[6],
                docx_preview_pdf_value,
                docx_preview_html_value,
                searchable_pdf_path,
                update[7],
                update[8],
            )
            yield update

    async def convert_batch_to_markdown_stream(
        uploaded_files,
        source_folder,
        recursive,
        save_next_to_source,
        end_pages=10,
        is_ocr=False,
        formula_enable=True,
        table_enable=True,
        image_analysis=True,
        effort=DEFAULT_HYBRID_EFFORT,
        language="ch",
        backend="pipeline",
        url=None,
        request: gr.Request = None,
    ):
        """Process a folder or multiple uploaded files sequentially.

        Sequential execution deliberately respects the existing API/GPU
        limiter. A failure of one document is recorded in the final report and
        does not discard results already produced for the rest of the batch.
        """
        request_locale = resolve_request_locale(request)
        source_dir: Path | None = None
        try:
            if (source_folder or "").strip():
                source_dir = resolve_batch_source_directory(source_folder)
                files = find_batch_input_files(source_dir, recursive=bool(recursive))
            else:
                raw_files = uploaded_files if isinstance(uploaded_files, (list, tuple)) else [uploaded_files]
                files = []
                for raw_file in raw_files:
                    if not raw_file:
                        continue
                    file_path = Path(raw_file.get("path") if isinstance(raw_file, dict) else raw_file)
                    if file_path.is_file() and file_path.suffix.lower() in _BATCH_INPUT_SUFFIXES:
                        files.append(file_path)
                files.sort(key=lambda path: str(path).casefold())
        except Exception as exc:
            logger.warning(f"Failed to prepare batch input: {exc}")
            yield (
                f"**{i18n('batch_error')}**: {exc}",
                None,
                "",
            )
            return

        if not files:
            yield (f"**{i18n('batch_no_files')}**", None, "")
            return

        timestamp = time.strftime("%y%m%d_%H%M%S")
        workspace_root = Path("./output") / "batch" / f"{timestamp}_{uuid.uuid4().hex[:8]}"
        workspace_root.mkdir(parents=True, exist_ok=True)
        saved_result_dirs: list[Path] = []
        report_lines: list[str] = []
        failed_count = 0

        for index, source_file in enumerate(files, start=1):
            yield (
                f"**{i18n('batch_processing')} {index}/{len(files)}:** `{source_file.name}`",
                None,
                "\n".join(report_lines),
            )
            try:
                result = await _run_to_markdown_job(
                    file_path=str(source_file),
                    end_pages=end_pages,
                    is_ocr=is_ocr,
                    formula_enable=formula_enable,
                    table_enable=table_enable,
                    image_analysis=image_analysis,
                    effort=effort,
                    language=language,
                    backend=backend,
                    url=url,
                    api_url=api_url,
                    client_side_output_generation=client_side_output_generation,
                )
                local_md_dir = Path(result[6])
                file_name = result[7]
                await asyncio.to_thread(build_gradio_searchable_pdf_result, local_md_dir, file_name)

                if source_dir is not None and save_next_to_source:
                    saved_dir = await asyncio.to_thread(
                        save_batch_result_next_to_source, local_md_dir, source_file
                    )
                else:
                    saved_dir = local_md_dir
                saved_result_dirs.append(saved_dir)
                report_lines.append(f"✓ `{source_file.name}` → `{saved_dir}`")
            except Exception as exc:
                failed_count += 1
                logger.exception(f"Batch conversion failed for {source_file}")
                report_lines.append(f"✗ `{source_file.name}`: {exc}")

            yield (
                f"**{i18n('batch_progress')}** {index}/{len(files)}",
                None,
                "\n".join(report_lines),
            )

        archive_path = None
        if saved_result_dirs:
            archive_parent = source_dir if (source_dir is not None and save_next_to_source) else workspace_root
            archive_path = archive_parent / f"MinerU_batch_{timestamp}.zip"
            try:
                await asyncio.to_thread(create_batch_result_archive, saved_result_dirs, archive_path)
            except Exception as exc:
                logger.exception("Failed to create batch result archive")
                report_lines.append(f"✗ {i18n('batch_archive_error')}: {exc}")
                archive_path = None

        completed = len(saved_result_dirs)
        status = i18n('batch_completed') + f": {completed}/{len(files)}"
        if failed_count:
            status += f"; {i18n('batch_failed')}: {failed_count}"
        yield (f"**{status}**", str(archive_path) if archive_path else None, "\n".join(report_lines))

    suffixes = [f".{suffix}" for suffix in pdf_suffixes + image_suffixes + office_suffixes]
    _blocks_kwargs = {} if IS_GRADIO_6 else {"css": APP_CSS, "js": APP_JS}
    with gr.Blocks(**_blocks_kwargs) as demo:
        gr.HTML(render_header_html(i18n), elem_classes=["mineru-header-html"])
        gr.HTML(
            render_workspace_intro_html(i18n),
            elem_classes=["mineru-workspace-heading"],
        )
        # Путь и имя файла текущего результата на диске — нужны для ручного
        # сохранения правок в md_text/content_list_json (см. save_md_text_edit,
        # save_content_list_json_edit ниже).
        local_md_dir_state = gr.State(value=None)
        file_name_state = gr.State(value=None)
        markdown_render_edit_state = gr.State(value=False)
        # Состояния визуального редактора (вкладка "Визуальный редактор"):
        # выбранный блок, текущая страница и флаг "DOCX/PDF устарели после
        # ручной правки" (см. editor_apply_text_edit/editor_apply_table_edit
        # ниже, которые выставляют его в True).
        selected_block_state = gr.State(value=None)
        editor_page_state = gr.State(value=0)
        exports_dirty_state = gr.State(value=False)
        with gr.Row(elem_classes=["mineru-workspace-row"]):
            with gr.Column(variant='panel', scale=2, min_width=280, elem_classes=["mineru-control-column"]):
                gr.HTML(i18n("upload_file"), elem_classes=["mineru-file-label"])
                input_file = gr.File(
                    container=False,
                    show_label=False,
                    file_types=suffixes,
                    elem_classes=["mineru-upload-file"],
                )
                with gr.Accordion(i18n("batch_title"), open=False, elem_classes=["mineru-batch-panel"]):
                    gr.HTML(i18n("batch_files"), elem_classes=["mineru-file-label"])
                    batch_files = gr.File(
                        container=False,
                        show_label=False,
                        file_types=suffixes,
                        file_count="multiple",
                        elem_classes=["mineru-batch-files"],
                    )
                    batch_source_folder = gr.Textbox(
                        label=i18n("batch_source_folder"),
                        info=i18n("batch_source_folder_info"),
                        placeholder=r"D:\Documents\For recognition",
                        elem_classes=["mineru-batch-source-folder"],
                    )
                    with gr.Row():
                        batch_recursive = gr.Checkbox(
                            label=i18n("batch_recursive"), value=False
                        )
                        batch_save_next_to_source = gr.Checkbox(
                            label=i18n("batch_save_next_to_source"), value=True
                        )
                    batch_convert_bu = gr.Button(
                        i18n("batch_convert"), variant="primary", elem_classes=["mineru-batch-convert"]
                    )
                    gr.HTML(i18n("batch_result"), elem_classes=["mineru-file-label"])
                    batch_result_archive = gr.File(
                        container=False, show_label=False, interactive=False,
                        elem_classes=["mineru-batch-result"],
                    )
                    batch_status = gr.Markdown(value="", elem_classes=["mineru-batch-status"])
                    batch_report = gr.Code(
                        label=i18n("batch_report"), language="markdown", interactive=False,
                        visible=True, elem_classes=["mineru-batch-report"],
                    )
                preferred_option = DEFAULT_BACKEND
                backend = gr.Dropdown(
                    build_backend_choices(http_client_enable, i18n),
                    label=i18n("backend"),
                    value=preferred_option,
                    info=get_backend_info(preferred_option),
                    elem_classes=["mineru-backend-select"],
                )
                with gr.Row(
                    visible=frontend_managed_initial_visibility(is_http_client_backend(preferred_option)),
                    elem_classes=["mineru-client-options"],
                ):
                    url = gr.Textbox(
                        label=i18n("server_url"),
                        value='http://localhost:30000',
                        placeholder='http://localhost:30000',
                        info=i18n("server_url_info"),
                    )
                # 下面这些选项在上传 office 文件时会被自动隐藏
                with gr.Group() as options_group:
                    max_pages = gr.Slider(1, max_convert_pages, max_convert_pages, step=1, label=i18n("max_pages"))
                    gr.Button(
                        i18n("advanced_options"),
                        size="sm",
                        elem_classes=["mineru-advanced-open"],
                    )
                with gr.Row(elem_classes=["mineru-actions"]):
                    change_bu = gr.Button(i18n("convert"), variant="primary", scale=1, min_width=0)
                    clear_bu = gr.ClearButton(value=i18n("clear"), scale=1, min_width=0)
                gr.HTML(i18n("convert_result"), elem_classes=["mineru-file-label"])
                output_file = gr.File(
                    container=False,
                    show_label=False,
                    interactive=False,
                    elem_classes=["mineru-result-file"],
                )
                gr.HTML(i18n("download_docx"), elem_classes=["mineru-file-label"])
                output_docx = gr.File(
                    container=False,
                    show_label=False,
                    interactive=False,
                    elem_classes=["mineru-result-docx"],
                )
                gr.HTML(i18n("download_searchable_pdf"), elem_classes=["mineru-file-label"])
                output_searchable_pdf = gr.File(
                    container=False,
                    show_label=False,
                    interactive=False,
                    elem_classes=["mineru-result-searchable-pdf"],
                )
                status_panel = gr.HTML(
                    value=render_status_steps_html("", i18n),
                    label=i18n("convert_status"),
                    elem_classes=["mineru-status-panel"],
                )

            _doc_preview_label = "doc preview" if IS_GRADIO_6 else i18n("doc_preview")
            # preview_content_height 约束文档预览/Markdown 的内容区；gradio_pdf 的 height
            # 实际是单页 canvas 高度，需要单独扣除 label 和分页器占用，避免上传 PDF 后撑高预览块。
            preview_content_height = 775
            pdf_preview_page_height = 720
            with gr.Column(variant='panel', scale=4, min_width=340, elem_classes=["mineru-preview-pane"]):
                _docx_preview_label = "DOCX preview" if IS_GRADIO_6 else i18n("docx_preview")
                with gr.Tabs(elem_classes=["mineru-preview-tabs"]):
                    with gr.Tab("Document" if IS_GRADIO_6 else i18n("document_tab_title")):
                        doc_show = PDF(
                            show_label=False,
                            interactive=False,
                            visible=True,
                            height=pdf_preview_page_height,
                        )
                        office_html = gr.HTML(
                            value="",
                            visible=False,
                            min_height=preview_content_height,
                            elem_classes=["mineru-office-preview-html"],
                        )
                    # Не подменяем предпросмотр редактором автоматически: в Gradio 6
                    # большая цепочка обновлений могла скрыть PDF раньше, чем успевал
                    # отобразиться editor_group. Редактор живёт в отдельной вкладке,
                    # поэтому вкладка «Документ» всегда остаётся стабильным просмотром
                    # исходного/размеченного файла.
                    with gr.Tab(i18n("visual_editor_tab")) as visual_editor_tab:
                        with gr.Column(visible=False, elem_classes=["mineru-editor-group"]) as editor_group:
                            with gr.Row(elem_classes=["mineru-editor-nav"]):
                                editor_prev_bu = gr.Button(
                                    "◀", size="sm", scale=0, min_width=44, elem_classes=["mineru-editor-prev"]
                                )
                                editor_page_label_html = gr.HTML(
                                    value="", elem_classes=["mineru-editor-page-label-html"]
                                )
                                editor_next_bu = gr.Button(
                                    "▶", size="sm", scale=0, min_width=44, elem_classes=["mineru-editor-next"]
                                )
                            with gr.Row(elem_classes=["mineru-editor-zoom-nav"]):
                                editor_zoom_out_bu = gr.Button("−", size="sm", scale=0, min_width=40)
                                editor_zoom_in_bu = gr.Button("+", size="sm", scale=0, min_width=40)
                                editor_zoom_fit_bu = gr.Button(
                                    i18n("editor_zoom_fit"), size="sm", scale=0, min_width=40
                                )
                                editor_zoom_actual_bu = gr.Button("1:1", size="sm", scale=0, min_width=40)
                            gr.HTML(
                                i18n("editor_click_hint"),
                                elem_classes=["mineru-editor-click-hint"],
                            )
                            with gr.Accordion(
                                i18n("editor_thumbnails_title"),
                                open=True,
                                elem_classes=["mineru-editor-thumbnails-accordion"],
                            ):
                                editor_thumbnails_gallery = gr.Gallery(
                                    show_label=False,
                                    allow_preview=False,
                                    columns=5,
                                    rows=3,
                                    height=280,
                                    elem_classes=["mineru-editor-thumbnails"],
                                )
                            editor_table_picker = gr.Dropdown(
                                choices=[],
                                value=None,
                                label=i18n("editor_table_picker"),
                                interactive=True,
                                visible=False,
                                elem_classes=["mineru-editor-table-picker"],
                            )
                            _editor_image_kwargs = {"buttons": []} if IS_GRADIO_6 else {
                                "show_download_button": False,
                                "show_fullscreen_button": False,
                                "show_share_button": False,
                            }
                            with gr.Row(elem_classes=["mineru-editor-workspace"]):
                                with gr.Column(scale=3, min_width=420, elem_classes=["mineru-editor-canvas"]):
                                    editor_image = gr.Image(
                                        # Координаты клика передаёт собственный обработчик
                                        # в gradio_app.js. На .select() в Gradio 6 полагаться
                                        # нельзя: событие не приходит для части рендеров Image.
                                        interactive=False,
                                        show_label=False,
                                        elem_classes=["mineru-editor-page"],
                                        **_editor_image_kwargs,
                                    )
                                    # Relay-канал «клик в браузере -> сервер»: обработчик в
                                    # gradio_app.js пишет JSON с координатами клика (или
                                    # индексом миниатюры/пункта пикера таблиц) в этот
                                    # CSS-скрытый textbox и диспатчит нативное событие
                                    # "input". Прежняя связка «прозрачная кнопка поверх
                                    # картинки + gr.State + js=-препроцессор» зависела от
                                    # трёх хрупких звеньев Gradio 6 сразу (геометрия
                                    # оверлея, elem_id на кнопке, доставка синтетического
                                    # click в Svelte-делегирование) и в живом браузере не
                                    # давала даже queue/join.
                                    #
                                    # Серверная привязка (ниже, editor_click_relay_tb.change)
                                    # -- ИМЕННО .change(), не .input(): в gradio@6.8.0
                                    # Textbox.svelte колбэк .input() вызывается только из
                                    # обработчика нативного "keypress" (реальные нажатия
                                    # клавиш), а наш JS шлёт только "input" на программно
                                    # изменённое значение -- до .input() он в принципе не
                                    # доходит. .change() же вызывается реактивным эффектом
                                    # на bind:value при ЛЮБОМ изменении значения независимо
                                    # от источника -- этим объяснялся живой симптом «relay
                                    # шлёт корректный JSON в textbox, но ни один клик не
                                    # уходит на сервер» (обнаружено сверкой с исходником
                                    # Svelte-компонента, не только логами). Прячем через
                                    # CSS, а не visible=False, чтобы элемент гарантированно
                                    # был в DOM.
                                    editor_click_relay_tb = gr.Textbox(
                                        value="",
                                        interactive=True,
                                        show_label=False,
                                        container=False,
                                        elem_id="mineru-editor-click-relay",
                                        elem_classes=["mineru-editor-click-relay"],
                                    )
                                with gr.Column(scale=2, min_width=290, elem_classes=["mineru-editor-side"]):
                                    with gr.Group(visible=False, elem_classes=["mineru-editor-block-panel"]) as editor_block_panel:
                                        editor_block_panel_title_html = gr.HTML(
                                            value=i18n("editor_block_panel_title"),
                                            elem_classes=["mineru-editor-block-panel-title"],
                                        )
                                        editor_block_snippet = gr.Image(
                                            interactive=False,
                                            show_label=False,
                                            elem_classes=["mineru-editor-block-snippet"],
                                            **_editor_image_kwargs,
                                        )
                                        editor_block_textbox = gr.Textbox(
                                            lines=8,
                                            show_label=False,
                                            visible=False,
                                            elem_classes=["mineru-editor-block-text"],
                                        )
                                        editor_table_html = gr.HTML(
                                            value="",
                                            visible=False,
                                            elem_classes=["mineru-editor-block-table"],
                                        )
                                        editor_table_harvest_tb = gr.State(value="")
                                        with gr.Row():
                                            editor_apply_bu = gr.Button(
                                                i18n("editor_apply_edit"),
                                                size="sm",
                                                variant="primary",
                                                visible=False,
                                                elem_classes=["mineru-editor-apply"],
                                            )
                                            editor_deselect_bu = gr.Button(
                                                i18n("editor_deselect"),
                                                size="sm",
                                                elem_classes=["mineru-editor-deselect"],
                                            )
                            editor_status_html = gr.HTML(
                                value="",
                                elem_id="mineru-editor-status",
                                elem_classes=["mineru-editor-status"],
                            )
                            with gr.Row(elem_classes=["mineru-editor-exports-row"]):
                                editor_refresh_exports_bu = gr.Button(
                                    i18n("editor_refresh_exports"),
                                    size="sm",
                                    elem_classes=["mineru-editor-refresh-exports"],
                                )
                                editor_next_suspect_bu = gr.Button(
                                    i18n("editor_next_suspect"),
                                    size="sm",
                                    elem_classes=["mineru-editor-next-suspect"],
                                )
                                editor_dirty_html = gr.HTML(
                                    value="", elem_classes=["mineru-editor-dirty"]
                                )
                    with gr.Tab(_docx_preview_label):
                        docx_preview_pdf = PDF(
                            label=_docx_preview_label,
                            interactive=False,
                            visible=False,
                            height=pdf_preview_page_height,
                            elem_classes=["mineru-docx-preview-pdf"],
                        )
                        docx_preview_html = gr.HTML(
                            value="",
                            visible=False,
                            label=i18n("docx_preview"),
                            min_height=preview_content_height,
                            elem_classes=["mineru-docx-preview-html"],
                        )

            with gr.Column(variant='panel', scale=4, min_width=340, elem_classes=["mineru-markdown-pane"]):
                _md_copy_kwargs = {"buttons": ["copy"]} if IS_GRADIO_6 else {"show_copy_button": True}
                _textarea_copy_kwargs = {"buttons": ["copy"]} if IS_GRADIO_6 else {"show_copy_button": True}
                with gr.Tabs(elem_classes=["mineru-markdown-tabs"]):
                    with gr.Tab(i18n("md_rendering")):
                        with gr.Row(elem_classes=["mineru-markdown-render-actions"]):
                            md_render_edit_bu = gr.Button(
                                i18n("edit_markdown_rendering"), size="sm", scale=0
                            )
                            md_render_save_bu = gr.Button(
                                i18n("save_markdown_rendering"),
                                size="sm",
                                variant="primary",
                                scale=0,
                                visible=False,
                            )
                        md = gr.Markdown(
                            label=i18n("md_rendering"),
                            height=preview_content_height,
                            elem_classes=["mineru-markdown-output"],
                            latex_delimiters=latex_delimiters,
                            line_breaks=True,
                            **_md_copy_kwargs
                        )
                        md_render_text = gr.Code(
                            lines=16,
                            language="markdown",
                            label=i18n("md_text"),
                            interactive=True,
                            wrap_lines=True,
                            show_label=False,
                            visible=False,
                            elem_classes=["mineru-markdown-render-editor"],
                        )
                    with gr.Tab(i18n("md_text")):
                        md_text = gr.Code(
                            lines=28,
                            language="markdown",
                            label=i18n("md_text"),
                            interactive=True,
                            wrap_lines=True,
                            show_label=False,
                            elem_classes=["mineru-markdown-text"],
                        )
                        save_md_text_bu = gr.Button(
                            i18n("save_button"),
                            size="sm",
                            elem_classes=["mineru-save-md-text"],
                        )
                    with gr.Tab(i18n("content_list_json")):
                        content_list_json = gr.Code(
                            lines=28,
                            language="json",
                            label=i18n("content_list_json"),
                            interactive=True,
                            wrap_lines=True,
                            show_label=False,
                            elem_classes=["mineru-content-list-json"],
                        )
                        save_content_list_json_bu = gr.Button(
                            i18n("save_button"),
                            size="sm",
                            elem_classes=["mineru-save-content-list-json"],
                        )
        if example_enable:
            example_root = os.path.join(os.getcwd(), 'examples')
            if os.path.exists(example_root):
                example_files = [
                    os.path.join(example_root, _) for _ in os.listdir(example_root)
                    if _.endswith(tuple(suffixes))
                ]
                if example_files:
                    with gr.Accordion(i18n("examples"), open=True, elem_classes=["mineru-examples-panel"]):
                        gr.Examples(
                            examples=example_files,
                            inputs=input_file,
                            elem_id="mineru-example-files",
                            label=None,
                        )

        with gr.Column(elem_classes=["mineru-advanced-popover"]):
            with gr.Column(elem_classes=["mineru-advanced-card"]):
                with gr.Group():
                    table_enable = gr.Checkbox(label=i18n("table_enable"), value=True, info=i18n("table_info"))
                    formula_enable = gr.Checkbox(label=get_formula_label(preferred_option), value=True, info=get_formula_info(preferred_option))
                    image_analysis = gr.Checkbox(
                        label=i18n("image_analysis_enable"),
                        value=True,
                        visible=frontend_managed_initial_visibility(
                            is_image_analysis_option_visible(preferred_option, DEFAULT_HYBRID_EFFORT)
                        ),
                        info=i18n("image_analysis_info"),
                        elem_classes=["mineru-image-analysis-option"],
                    )
                    with gr.Column(elem_classes=["mineru-hybrid-effort-option"]):
                        hybrid_effort = gr.Radio(
                            list(HYBRID_EFFORT_CHOICES),
                            label=i18n("hybrid_effort"),
                            value=DEFAULT_HYBRID_EFFORT,
                            info=i18n("hybrid_effort_info"),
                            elem_classes=["mineru-hybrid-effort"],
                        )
                with gr.Column(elem_classes=["mineru-force-ocr-option"]):
                    with gr.Group():
                        with gr.Column(elem_classes=["mineru-ocr-language-options"]):
                            language = gr.Dropdown(
                                all_lang,
                                label=i18n("ocr_language"),
                                value=all_lang[0],
                                info=i18n("ocr_language_info"),
                            )
                        is_ocr = gr.Checkbox(
                            label=i18n("force_ocr"),
                            value=False,
                            info=i18n(select_force_ocr_info_key(preferred_option)),
                        )

        # 添加事件处理
        _private_api_kwargs = (
            {"api_visibility": "private", "queue": False, "show_progress": "hidden"}
            if IS_GRADIO_6
            else {"api_name": False, "queue": False, "show_progress": "hidden"}
        )
        backend.change(
            fn=update_interface,
            inputs=[backend, hybrid_effort],
            outputs=[is_ocr, formula_enable, backend],
            **_private_api_kwargs
        )
        # 添加demo.load事件，在页面加载时触发一次界面更新
        demo.load(
            fn=update_interface,
            inputs=[backend, hybrid_effort],
            outputs=[is_ocr, formula_enable, backend],
            **_private_api_kwargs
        )
        clear_bu.add([
            input_file, md, doc_show, md_text, content_list_json, output_file, output_docx,
            output_searchable_pdf, is_ocr, office_html, docx_preview_html, status_panel,
            local_md_dir_state, file_name_state, selected_block_state, editor_page_state,
            exports_dirty_state, batch_files, batch_source_folder, batch_recursive,
            batch_save_next_to_source, batch_result_archive, batch_status, batch_report,
        ])

        def reset_primary_ui():
            """清除主界面状态。高级气泡由前端点击外部逻辑自动收起。"""
            return (
                gr.update(visible=True),
                gr.update(value=None, visible=True),
                gr.update(value="", visible=False),
                gr.update(value=render_status_steps_html("", i18n)),
                gr.update(value=""),
                gr.update(visible=False),
                gr.update(value="", visible=False),
                gr.update(value=None),
                gr.update(visible=False),
            )

        # 清除按钮额外重置 UI 可见性（ClearButton 不一定触发 input_file.change）
        clear_bu.click(
            fn=reset_primary_ui,
            inputs=[],
            outputs=[options_group, doc_show, office_html, status_panel, content_list_json, docx_preview_pdf, docx_preview_html, output_searchable_pdf, editor_group],
            **_private_api_kwargs
        )

        def update_file_options_html_for_ui(file_path, request: gr.Request):
            """绑定当前 i18n 的文件上传 UI 更新函数，避免事件签名暴露额外参数。
            新文件上传时同时隐藏可视化编辑器（用户在旧结果之上加载新文档）。"""
            options_group_update, office_html_update = update_file_options_html(file_path, request, i18n)
            return options_group_update, office_html_update, gr.update(visible=False)

        # 第一阶段：快速更新 options_group 和 office_html，不涉及 gradio_pdf 组件
        # 第二阶段（.then）：单独更新 doc_show，使 office_html 的 processing 遮罩
        # 在第一阶段完成后立即消失，规避 gradio_pdf 0.0.24 与 Gradio 6 的兼容性问题。
        input_file.change(
            fn=update_file_options_html_for_ui,
            inputs=input_file,
            outputs=[options_group, office_html, editor_group],
            **_private_api_kwargs
        ).then(
            fn=update_doc_show,
            inputs=input_file,
            outputs=[doc_show],
            **_private_api_kwargs
        )
        _to_md_api_kwargs = (
            {
                "api_visibility": "public" if api_enable else "private",
                "queue": True,
                "show_progress": "hidden",
            }
            if IS_GRADIO_6
            else {
                "api_name": "to_markdown" if api_enable else False,
                "queue": True,
                "show_progress": "hidden",
            }
        )
        # .then(post_convert_document_view, ...) привязывается ниже, после
        # определения _editor_view_outputs/post_convert_document_view (секция
        # "Визуальный редактор" объявлена дальше по файлу).
        _change_bu_click_event = change_bu.click(
            fn=convert_to_markdown_stream,
            inputs=[input_file, max_pages, is_ocr, formula_enable, table_enable, image_analysis, hybrid_effort, language, backend, url],
            outputs=[status_panel, output_file, md, md_text, content_list_json, doc_show, output_docx, docx_preview_pdf, docx_preview_html, output_searchable_pdf, local_md_dir_state, file_name_state],
            **_to_md_api_kwargs
        )
        batch_convert_bu.click(
            fn=convert_batch_to_markdown_stream,
            inputs=[
                batch_files,
                batch_source_folder,
                batch_recursive,
                batch_save_next_to_source,
                max_pages,
                is_ocr,
                formula_enable,
                table_enable,
                image_analysis,
                hybrid_effort,
                language,
                backend,
                url,
            ],
            outputs=[batch_status, batch_result_archive, batch_report],
            **_to_md_api_kwargs,
        )

        def save_md_text_edit(md_text_value, local_md_dir, file_name, request: gr.Request = None):
            """Сохраняет отредактированный текст Markdown на диск и обновляет вкладку рендеринга."""
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return (
                    gr.skip(),
                    render_save_status_html(
                        i18n, "save_error_no_result", locale=request_locale, is_error=True
                    ),
                )
            try:
                md_path = Path(local_md_dir) / f"{file_name}.md"
                md_path.write_text(md_text_value or "", encoding="utf-8")
            except Exception as exc:
                logger.warning(f"Failed to save edited Markdown for {file_name}: {exc}")
                return (
                    gr.skip(),
                    render_save_status_html(
                        i18n, "save_md_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                )

            rendered_md = prepare_markdown_for_gradio_preview(
                replace_image_with_gradio_file_urls(md_text_value or "", local_md_dir),
                latex_delimiters,
            )
            timestamp = time.strftime("%H:%M:%S")
            return (
                gr.update(value=rendered_md),
                render_save_status_html(
                    i18n, "save_md_success", locale=request_locale, detail=f" ({timestamp})"
                ),
            )

        def toggle_markdown_render_edit(md_text_value, is_editing):
            """Открывает исходник под рендерингом, не скрывая предпросмотр таблиц."""
            editing = not bool(is_editing)
            return (
                gr.update(visible=True, height=420 if editing else preview_content_height),
                gr.update(value=md_text_value or "", visible=editing),
                gr.update(
                    value=i18n("close_markdown_editor") if editing else i18n("edit_markdown_rendering")
                ),
                gr.update(visible=editing),
                editing,
            )

        def save_markdown_render_edit(md_text_value, local_md_dir, file_name, request: gr.Request = None):
            """Сохраняет правку, сразу перерисовывая таблицы над открытым исходником."""
            rendered_md, status = save_md_text_edit(
                md_text_value, local_md_dir, file_name, request
            )
            return (
                gr.update(value=rendered_md, visible=True, height=420),
                gr.update(value=md_text_value or ""),
                gr.update(value=md_text_value or "", visible=True),
                gr.update(value=i18n("close_markdown_editor")),
                gr.update(visible=True),
                True,
                status,
            )

        def save_content_list_json_edit(content_list_json_value, local_md_dir, file_name, request: gr.Request = None):
            """Проверяет валидность JSON и сохраняет список содержимого на диск."""
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return render_save_status_html(
                    i18n, "save_error_no_result", locale=request_locale, is_error=True
                )
            try:
                parsed_json = json.loads(content_list_json_value or "")
            except Exception as exc:
                return render_save_status_html(
                    i18n, "save_json_invalid", locale=request_locale, detail=f": {exc}", is_error=True
                )
            try:
                content_list_path = Path(local_md_dir) / f"{file_name}_content_list.json"
                content_list_path.write_text(
                    json.dumps(parsed_json, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                logger.warning(f"Failed to save edited content list JSON for {file_name}: {exc}")
                return render_save_status_html(
                    i18n, "save_json_error", locale=request_locale, detail=f": {exc}", is_error=True
                )

            timestamp = time.strftime("%H:%M:%S")
            return render_save_status_html(
                i18n, "save_json_success", locale=request_locale, detail=f" ({timestamp})"
            )

        save_md_text_bu.click(
            fn=save_md_text_edit,
            inputs=[md_text, local_md_dir_state, file_name_state],
            outputs=[md, status_panel],
            **_private_api_kwargs
        )
        md_render_edit_bu.click(
            fn=toggle_markdown_render_edit,
            inputs=[md_text, markdown_render_edit_state],
            outputs=[
                md,
                md_render_text,
                md_render_edit_bu,
                md_render_save_bu,
                markdown_render_edit_state,
            ],
            **_private_api_kwargs,
        )
        md_render_save_bu.click(
            fn=save_markdown_render_edit,
            inputs=[md_render_text, local_md_dir_state, file_name_state],
            outputs=[
                md,
                md_text,
                md_render_text,
                md_render_edit_bu,
                md_render_save_bu,
                markdown_render_edit_state,
                status_panel,
            ],
            **_private_api_kwargs,
        )
        save_content_list_json_bu.click(
            fn=save_content_list_json_edit,
            inputs=[content_list_json, local_md_dir_state, file_name_state],
            outputs=[status_panel],
            **_private_api_kwargs
        )

        # --- Визуальный редактор -------------------------------------------------
        # Стандартный набор из 11 выходов для навигации/инициализации/снятия
        # выбора: картинка с оверлеем, подпись страницы, статус, состояние
        # страницы, состояние выбранного блока и панель блока (видимость +
        # значения текстового/табличного редактора + видимость кнопок «Применить»
        # + кроп оригинала выбранного блока).
        _editor_view_outputs = [
            editor_image,
            editor_page_label_html,
            editor_status_html,
            editor_page_state,
            selected_block_state,
            editor_block_panel,
            editor_block_textbox,
            editor_table_html,
            editor_apply_bu,
            editor_block_snippet,
        ]

        def _editor_panel_closed():
            return (
                gr.update(visible=False),
                gr.update(visible=False, value=""),
                gr.update(visible=False, value=""),
                gr.update(visible=False),
                gr.update(visible=False, value=None),
            )

        def _editor_panel_text(text_value, snippet_img):
            return (
                gr.update(visible=True),
                gr.update(visible=True, value=text_value),
                gr.update(visible=False, value=""),
                gr.update(visible=True),
                gr.update(visible=True, value=snippet_img),
            )

        def _editor_panel_table(table_html_value, snippet_img):
            return (
                gr.update(visible=True),
                gr.update(visible=False, value=""),
                gr.update(visible=True, value=table_html_value),
                gr.update(visible=True),
                gr.update(visible=True, value=snippet_img),
            )

        def _render_editor_page(local_md_dir, file_name, page_idx, selected_idx, request_locale):
            """Загружает middle.json заново и строит (картинка-с-оверлеем, подпись страницы)."""
            middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
            total_pages = visual_editor.page_count(local_md_dir, file_name)
            blocks = visual_editor.list_page_blocks(middle_json, page_idx)
            page_size = visual_editor.get_page_size(middle_json, page_idx)
            base_img = visual_editor.render_page_base(local_md_dir, file_name, page_idx)
            overlay_img = visual_editor.draw_overlay(base_img, blocks, page_size, selected_idx)
            label_html = render_editor_page_label_html(i18n, page_idx, total_pages, request_locale)
            return overlay_img, label_html

        def init_visual_editor(local_md_dir, file_name, request: gr.Request = None):
            """Инициализирует редактор при выборе вкладки: страница 0, без выделения."""
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return (
                    None,
                    "",
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    0,
                    None,
                ) + _editor_panel_closed()
            if not visual_editor.has_origin_pdf(local_md_dir, file_name):
                return (
                    None,
                    "",
                    render_save_status_html(
                        i18n, "editor_no_origin_pdf", locale=request_locale, is_error=True
                    ),
                    0,
                    None,
                ) + _editor_panel_closed()
            try:
                overlay_img, label_html = _render_editor_page(
                    local_md_dir, file_name, 0, None, request_locale
                )
            except Exception as exc:
                logger.warning(f"Failed to initialize visual editor for {file_name}: {exc}")
                return (
                    None,
                    "",
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    0,
                    None,
                ) + _editor_panel_closed()
            return (overlay_img, label_html, "", 0, None) + _editor_panel_closed()

        def _editor_table_selection_keys(middle_json):
            """Ключи ``page::selection_id`` всех таблиц документа в едином
            детерминированном порядке обхода. Общий источник и для choices
            пикера, и для fallback по ``SelectData.index`` в обработчике
            выбора -- индекс пункта в списке гарантированно бьётся с ключом."""
            keys = []
            pages = middle_json.get("pdf_info") or []
            for page_idx in range(len(pages)):
                for block in visual_editor.list_page_blocks(middle_json, page_idx):
                    if block.get("kind") != "table":
                        continue
                    keys.append(f"{page_idx}::{block['selection_id']}")
            return keys

        def _build_editor_table_picker_update(local_md_dir, file_name, locale=None):
            """Строит надёжный список таблиц как альтернативу точному клику по странице.

            Текст choices резолвится на сервере через translate_ui: объект
            ``i18n(...)`` внутри f-строки сериализуется в сырой маркер
            ``__i18n__{...}`` -- фронтенд Gradio подменяет такие маркеры только
            когда ими является ВСЯ строка, поэтому в списке был виден мусор
            вида ``__i18n__{"__type__": ...} 5 — таблица 1`` в две строки
            (и из-за переноса плыл hit-box пунктов дропдауна)."""
            try:
                middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                page_word = translate_ui(i18n, "editor_page_word", locale)
                table_word = translate_ui(i18n, "editor_table_word", locale)
                choices = []
                for table_number, key in enumerate(
                    _editor_table_selection_keys(middle_json), start=1
                ):
                    page_value = key.split("::", 1)[0]
                    choices.append(
                        (
                            f"{page_word} {int(page_value) + 1} — {table_word} {table_number}",
                            key,
                        )
                    )
                return gr.update(choices=choices, value=None, visible=bool(choices))
            except Exception as exc:
                logger.warning(f"Failed to build editor table picker for {file_name}: {exc}")
                return gr.update(choices=[], value=None, visible=False)

        def _build_editor_thumbnails_update(local_md_dir, file_name):
            """Свежая лента миниатюр текущего документа (или пустая при сбое)."""
            try:
                return gr.update(value=visual_editor.list_thumbnails(local_md_dir, file_name))
            except Exception as exc:
                logger.warning(f"Failed to build editor thumbnails for {file_name}: {exc}")
                return gr.update(value=[])

        def post_convert_document_view(local_md_dir, file_name, request: gr.Request = None):
            """Готовит отдельную вкладку визуального редактора после конвертации.

            Обычный предпросмотр документа намеренно не входит в outputs: он должен
            оставаться видимым, даже если инициализация редактора или миниатюр дала
            сбой. Это устраняет сценарий «предпросмотр появился и сразу пропал».
            """
            editor_outputs = init_visual_editor(local_md_dir, file_name, request)
            has_origin = bool(
                local_md_dir and file_name and visual_editor.has_origin_pdf(local_md_dir, file_name)
            )
            if has_origin:
                editor_group_update = gr.update(visible=True)
                gallery_update = _build_editor_thumbnails_update(local_md_dir, file_name)
                table_picker_update = _build_editor_table_picker_update(
                    local_md_dir, file_name, resolve_request_locale(request)
                )
            else:
                editor_group_update = gr.update(visible=False)
                gallery_update = gr.update(value=[])
                table_picker_update = gr.update(choices=[], value=None, visible=False)
            return editor_outputs + (editor_group_update, gallery_update, table_picker_update)

        # Привязка отложена сюда, т.к. _editor_view_outputs/post_convert_document_view
        # объявляются только в этой секции, уже после change_bu.click(...) выше.
        #
        # ВАЖНО: это событие отдаёт разнородные выходы (Image/Gallery/Column-visible/
        # State), включая переключение видимости editor_group. Замер 2026-07-15 показал, что
        # с queue=False (как в _private_api_kwargs) применение части выходов на клиенте
        # ненадёжно/неатомарно (voспроизведено дважды подряд в реальном браузере:
        # 1-й прогон — editor_group и editor_thumbnails_gallery не применились вовсе,
        # 2-й прогон — применился editor_group, но не editor_thumbnails_gallery).
        # У единственного другого события с сопоставимо большим и разнородным списком
        # выходов в этом файле (convert_to_markdown_stream, dep с queue=True) такой
        # проблемы не было — все 12 выходов применились. Поэтому здесь queue=True
        # вместо _private_api_kwargs (api-видимость остаётся приватной).
        _editor_view_api_kwargs = (
            {"api_visibility": "private", "queue": True, "show_progress": "hidden"}
            if IS_GRADIO_6
            else {"api_name": False, "queue": True, "show_progress": "hidden"}
        )

        def open_visual_editor_tab(
            local_md_dir, file_name, current_page_idx, selected, request: gr.Request = None
        ):
            """Загружает редактор именно при открытии вкладки.

            Инициализация после конвертации может прийти в браузер раньше,
            чем Gradio смонтирует скрытую вкладку. Повторная, дешёвая загрузка
            по событию ``Tab.select`` устраняет пустую вкладку без повторного
            распознавания документа.

            Возврат на вкладку НЕ сбрасывает страницу и выделение: полный
            сброс на страницу 0 делает только ``post_convert_document_view``
            после новой конвертации (единственный путь, меняющий
            ``local_md_dir_state``). Раньше безусловный ``init_visual_editor``
            откатывал редактор на страницу 0 при каждом входе -- пользователь,
            вернувшийся с другой вкладки, кликал по "своей" странице, а
            состояние уже указывало на первую.
            """
            request_locale = resolve_request_locale(request)
            visible = bool(
                local_md_dir and file_name and visual_editor.has_origin_pdf(local_md_dir, file_name)
            )
            table_picker_update = (
                _build_editor_table_picker_update(local_md_dir, file_name, request_locale)
                if visible
                else gr.update(choices=[], value=None, visible=False)
            )
            # Ленту миниатюр перестраиваем и при каждом входе на вкладку: апдейт
            # галереи из post_convert_document_view (13 разнородных выходов на
            # скрытую вкладку) на клиенте применяется ненадёжно -- живой симптом:
            # после конвертации НОВОГО документа лента оставалась от старого.
            # Вкладочный рендер -- гарантированный ремонтный путь.
            thumbnails_update = (
                _build_editor_thumbnails_update(local_md_dir, file_name)
                if visible
                else gr.update(value=[])
            )
            if not visible:
                editor_outputs = init_visual_editor(local_md_dir, file_name, request)
                return editor_outputs + (
                    gr.update(visible=False), thumbnails_update, table_picker_update
                )
            try:
                total_pages = visual_editor.page_count(local_md_dir, file_name)
                page_idx = max(0, min(max(total_pages - 1, 0), int(current_page_idx or 0)))
                selected_valid = False
                if isinstance(selected, dict) and selected.get("page_idx") == page_idx:
                    middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                    blocks = visual_editor.list_page_blocks(middle_json, page_idx)
                    selected_valid = any(
                        b["block_idx"] == selected.get("block_idx")
                        and b["source"] == selected.get("source", "para_blocks")
                        for b in blocks
                    )
                overlay_img, label_html = _render_editor_page(
                    local_md_dir,
                    file_name,
                    page_idx,
                    selected if selected_valid else None,
                    request_locale,
                )
            except Exception as exc:
                logger.warning(f"Failed to restore visual editor state for {file_name}: {exc}")
                editor_outputs = init_visual_editor(local_md_dir, file_name, request)
                return editor_outputs + (
                    gr.update(visible=True), thumbnails_update, table_picker_update
                )
            # Панель блока не трогаем при живом выделении (gr.skip) -- её
            # содержимое уже показывает выбранный блок; при потерянном
            # выделении закрываем, чтобы панель не показывала чужой текст.
            panel_updates = (gr.skip(),) * 5 if selected_valid else _editor_panel_closed()
            return (
                overlay_img,
                label_html,
                gr.skip(),
                page_idx,
                selected if selected_valid else None,
            ) + panel_updates + (
                gr.update(visible=True), thumbnails_update, table_picker_update
            )

        _change_bu_click_event.then(
            fn=post_convert_document_view,
            inputs=[local_md_dir_state, file_name_state],
            outputs=_editor_view_outputs + [editor_group, editor_thumbnails_gallery, editor_table_picker],
            **_editor_view_api_kwargs,
        )
        # Оба события пишут в _editor_view_outputs, но гонка неопасна:
        # open_visual_editor_tab теперь не деструктивен (рендерит из текущих
        # состояний), а единственный деструктивный сброс -- post_convert_document_view
        # -- всегда последний писатель после конвертации.
        visual_editor_tab.select(
            fn=open_visual_editor_tab,
            inputs=[local_md_dir_state, file_name_state, editor_page_state, selected_block_state],
            outputs=_editor_view_outputs + [
                editor_group, editor_thumbnails_gallery, editor_table_picker
            ],
            **_editor_view_api_kwargs,
        )

        def _editor_navigate(local_md_dir, file_name, current_page_idx, delta, request_locale):
            if not local_md_dir or not file_name:
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    current_page_idx,
                    None,
                ) + _editor_panel_closed()
            try:
                total_pages = visual_editor.page_count(local_md_dir, file_name)
            except Exception:
                total_pages = 0
            if total_pages <= 0:
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_no_origin_pdf", locale=request_locale, is_error=True
                    ),
                    current_page_idx,
                    None,
                ) + _editor_panel_closed()
            new_page_idx = max(0, min(total_pages - 1, (current_page_idx or 0) + delta))
            try:
                overlay_img, label_html = _render_editor_page(
                    local_md_dir, file_name, new_page_idx, None, request_locale
                )
            except Exception as exc:
                logger.warning(f"Failed to render editor page {new_page_idx} for {file_name}: {exc}")
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    current_page_idx,
                    None,
                ) + _editor_panel_closed()
            return (overlay_img, label_html, "", new_page_idx, None) + _editor_panel_closed()

        def editor_prev(local_md_dir, file_name, current_page_idx, request: gr.Request = None):
            return _editor_navigate(
                local_md_dir, file_name, current_page_idx, -1, resolve_request_locale(request)
            )

        def editor_next(local_md_dir, file_name, current_page_idx, request: gr.Request = None):
            return _editor_navigate(
                local_md_dir, file_name, current_page_idx, 1, resolve_request_locale(request)
            )

        def editor_thumbnail_select(local_md_dir, file_name, evt: gr.SelectData, request: gr.Request = None):
            """Клик по миниатюре: переход на эту страницу (абсолютный индекс,
            в отличие от editor_prev/editor_next с дельтой), без выбора блока."""
            request_locale = resolve_request_locale(request)
            target_idx = evt.index if evt is not None else 0
            if not local_md_dir or not file_name:
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    target_idx, None,
                ) + _editor_panel_closed()
            try:
                overlay_img, label_html = _render_editor_page(
                    local_md_dir, file_name, target_idx, None, request_locale
                )
            except Exception as exc:
                logger.warning(f"Failed to jump to page {target_idx} for {file_name}: {exc}")
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    target_idx, None,
                ) + _editor_panel_closed()
            return (overlay_img, label_html, "", target_idx, None) + _editor_panel_closed()

        def editor_deselect(local_md_dir, file_name, page_idx, request: gr.Request = None):
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return (
                    gr.skip(), gr.skip(), gr.skip(), page_idx, None
                ) + _editor_panel_closed()
            try:
                overlay_img, label_html = _render_editor_page(
                    local_md_dir, file_name, page_idx, None, request_locale
                )
            except Exception as exc:
                logger.warning(f"Failed to redraw editor page after deselect: {exc}")
                return (
                    gr.skip(), gr.skip(), gr.skip(), page_idx, None
                ) + _editor_panel_closed()
            return (overlay_img, label_html, "", page_idx, None) + _editor_panel_closed()

        def _select_block_outputs(local_md_dir, file_name, page_idx, selection_id, request_locale):
            """Общий путь построения 11-tuple вывода редактора для уже найденного
            block_idx -- используется и кликом по картинке, и кнопкой "следующий
            подозрительный". Заново читает middle.json/blocks/base_img (небольшая
            повторная работа по сравнению с вызовом из editor_image_select, где эти
            же данные уже есть локально -- осознанный компромисс ради простого и
            независимого API этой функции)."""
            middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
            blocks = visual_editor.list_page_blocks(middle_json, page_idx)
            page_size = visual_editor.get_page_size(middle_json, page_idx)
            base_img = visual_editor.render_page_base(local_md_dir, file_name, page_idx)
            total_pages = visual_editor.page_count(local_md_dir, file_name)
            label_html = render_editor_page_label_html(i18n, page_idx, total_pages, request_locale)

            block_meta = next((b for b in blocks if b["selection_id"] == selection_id), None)
            if block_meta is None:
                raise ValueError(f"Selected block not found: {selection_id}")
            kind = block_meta.get("kind") if block_meta else "other"
            source = block_meta.get("source", "para_blocks")
            block_idx = block_meta["block_idx"]
            selected = {
                "page_idx": page_idx,
                "block_idx": block_idx,
                "source": source,
                "kind": kind,
            }
            overlay_img = visual_editor.draw_overlay(base_img, blocks, page_size, selected)
            raw_block = middle_json["pdf_info"][page_idx][source][block_idx]

            bbox = block_meta.get("bbox") if block_meta else None
            try:
                snippet_img = visual_editor.get_block_snippet(base_img, page_size, bbox) if bbox else None
            except Exception as exc:
                logger.warning(f"Failed to build block snippet for {file_name}: {exc}")
                snippet_img = None

            if kind == "text":
                text_value = visual_editor.get_block_text(raw_block)
                return (
                    overlay_img,
                    label_html,
                    render_save_status_html(i18n, "editor_block_selected", locale=request_locale),
                    page_idx,
                    selected,
                ) + _editor_panel_text(
                    text_value, snippet_img
                )
            if kind == "table":
                table_span = visual_editor.recover_table_span_from_model(
                    local_md_dir, file_name, page_idx, raw_block, page_size
                )
                if not table_span:
                    return (
                        overlay_img,
                        label_html,
                        render_save_status_html(
                            i18n, "editor_block_no_text", locale=request_locale
                        ),
                        page_idx,
                        None,
                    ) + _editor_panel_closed()
                table_html_value = visual_editor.make_editable_table_html(table_span["html"])
                return (
                    overlay_img,
                    label_html,
                    render_save_status_html(i18n, "editor_block_selected", locale=request_locale),
                    page_idx,
                    selected,
                ) + _editor_panel_table(
                    table_html_value, snippet_img
                )

            # image/interline_equation и т.п.: нет редактируемого текста
            return (
                overlay_img,
                label_html,
                render_save_status_html(i18n, "editor_block_no_text", locale=request_locale),
                page_idx,
                None,
            ) + _editor_panel_closed()

        def editor_image_coordinate_select(
            click_xy_json, local_md_dir, file_name, page_idx, request: gr.Request = None
        ):
            """Диспетчер relay-канала кликов из ``gradio_app.js``.

            Payload -- JSON из скрытого textbox: ``{"x":..,"y":..,"t":..}`` --
            клик по странице (hit-test блока), ``{"thumb":N,"t":..}`` -- клик по
            миниатюре (абсолютный переход на страницу N), ``{"table_option_index":N,"t":..}``
            -- выбор пункта в пикере таблиц по позиции в исходном (нефильтрованном)
            списке choices (``data-index`` пункта Dropdown -- см. комментарий у JS
            mousedown-обработчика). Поле ``t`` -- nonce, гарантирующий изменение
            значения textbox (без него повторный клик в ту же точку/миниатюру не
            рождал бы событие input). В отличие от ``gr.Image.select``/
            ``gr.Gallery.select``/``gr.Dropdown.change`` этот путь не зависит от
            внутренних событий Gradio 6, которые не приходят на части рендеров
            (у Gallery часть кликов давала ноль событий, часть -- два; у пикера
            таблиц значение визуально менялось, но ``.change`` не долетал вовсе).
            """
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    page_idx,
                    None,
                ) + _editor_panel_closed()
            try:
                click_payload = json.loads(click_xy_json or "")
                if "thumb" in click_payload:
                    total_pages = visual_editor.page_count(local_md_dir, file_name)
                    target_idx = max(
                        0, min(max(total_pages - 1, 0), int(click_payload["thumb"]))
                    )
                    overlay_img, label_html = _render_editor_page(
                        local_md_dir, file_name, target_idx, None, request_locale
                    )
                    return (
                        overlay_img, label_html, "", target_idx, None
                    ) + _editor_panel_closed()
                if "table_option_index" in click_payload:
                    middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                    keys = _editor_table_selection_keys(middle_json)
                    option_idx = int(click_payload["table_option_index"])
                    if option_idx < 0 or option_idx >= len(keys):
                        raise ValueError(f"Table picker index out of range: {option_idx}")
                    table_page_value, table_selection_id = keys[option_idx].split("::", 1)
                    try:
                        return _select_block_outputs(
                            local_md_dir,
                            file_name,
                            int(table_page_value),
                            table_selection_id,
                            request_locale,
                        )
                    except Exception:
                        logger.exception(f"Editor table picker selection failed for {file_name}")
                        return (
                            gr.skip(),
                            gr.skip(),
                            render_save_status_html(
                                i18n,
                                "editor_load_error",
                                locale=request_locale,
                                detail=f" ({time.strftime('%H:%M:%S')})",
                                is_error=True,
                            ),
                            page_idx,
                            None,
                        ) + _editor_panel_closed()
                middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                blocks = visual_editor.list_page_blocks(middle_json, page_idx)
                page_size = visual_editor.get_page_size(middle_json, page_idx)
                base_img = visual_editor.render_page_base(local_md_dir, file_name, page_idx)
                click_xy = (float(click_payload["x"]), float(click_payload["y"]))
                found_idx = visual_editor.hit_test(
                    blocks, page_size, base_img.size, click_xy[0], click_xy[1]
                )
            except Exception as exc:
                logger.warning(f"Editor hit-test failed for {file_name}: {exc}")
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    page_idx,
                    None,
                ) + _editor_panel_closed()

            total_pages = visual_editor.page_count(local_md_dir, file_name)
            label_html = render_editor_page_label_html(i18n, page_idx, total_pages, request_locale)

            if found_idx is None:
                overlay_img = visual_editor.draw_overlay(base_img, blocks, page_size, None)
                # Статус обязан быть непустым и меняющимся: клиентский спиннер
                # "Загрузка выбранного блока…" снимается MutationObserver'ом по
                # изменению DOM статуса. Пустая строка при прежнем пустом
                # значении не даёт мутации -- спиннер зависал, а клик выглядел
                # как "ничего не произошло".
                miss_status = render_save_status_html(
                    i18n,
                    "editor_no_block_at_click",
                    locale=request_locale,
                    detail=f" ({time.strftime('%H:%M:%S')})",
                    is_error=True,
                )
                return (overlay_img, label_html, miss_status, page_idx, None) + _editor_panel_closed()

            # Успешный hit-test раньше уходил в _select_block_outputs БЕЗ
            # try/except: любое исключение внутри (recover_table_span_from_model,
            # сборка HTML таблицы и т.п.) оставалось необработанным -- клиент
            # видел «join ушёл, панель не открылась» без какой-либо ошибки в UI.
            # Пока relay-канал не работал, эта ветка была недостижима и дыра
            # не проявлялась.
            try:
                return _select_block_outputs(
                    local_md_dir, file_name, page_idx, found_idx, request_locale
                )
            except Exception:
                logger.exception(f"Editor block selection failed for {file_name}")
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n,
                        "editor_load_error",
                        locale=request_locale,
                        detail=f" ({time.strftime('%H:%M:%S')})",
                        is_error=True,
                    ),
                    page_idx,
                    None,
                ) + _editor_panel_closed()

        def editor_table_picker_select(table_key, local_md_dir, file_name, request: gr.Request = None):
            """Открывает таблицу из обычного списка, не зависящего от Image.click в Gradio."""
            request_locale = resolve_request_locale(request)
            if not table_key or not local_md_dir or not file_name:
                # .change срабатывает и на программный сброс value=None при
                # входе на вкладку/после конвертации -- раньше здесь был
                # editor_deselect(..., 0, ...), который скрыто откатывал
                # редактор на страницу 0. Пустой выбор -- всегда no-op.
                return (gr.skip(),) * len(_editor_view_outputs)
            try:
                page_value, selection_id = str(table_key).split("::", 1)
                page_idx = int(page_value)
                return _select_block_outputs(
                    local_md_dir, file_name, page_idx, selection_id, request_locale
                )
            except Exception as exc:
                logger.warning(f"Failed to select table from picker for {file_name}: {exc}")
                return (
                    gr.skip(),
                    gr.skip(),
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    0,
                    None,
                ) + _editor_panel_closed()

        def editor_next_suspect(local_md_dir, file_name, page_idx, selected, request: gr.Request = None):
            """Ищет следующий блок с низким score (min_score < threshold) и
            переключает редактор на него -- та же логика, что editor_image_select,
            но старт поиска задаётся текущим выбором, а не кликом по картинке."""
            request_locale = resolve_request_locale(request)
            if not local_md_dir or not file_name:
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    page_idx, None,
                ) + _editor_panel_closed()
            try:
                middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                if not visual_editor.has_confidence_scores(middle_json):
                    return (
                        gr.skip(), gr.skip(),
                        render_save_status_html(i18n, "editor_suspect_unavailable", locale=request_locale),
                        page_idx, None,
                    ) + _editor_panel_closed()
                start_block_idx = (
                    selected.get("block_idx")
                    if selected and selected.get("page_idx") == page_idx
                    else None
                )
                result = visual_editor.find_next_suspect_block(middle_json, page_idx or 0, start_block_idx)
            except Exception as exc:
                logger.warning(f"Failed to search suspect blocks for {file_name}: {exc}")
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(
                        i18n, "editor_load_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    page_idx, None,
                ) + _editor_panel_closed()
            if result is None:
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(i18n, "editor_no_suspects", locale=request_locale),
                    page_idx, None,
                ) + _editor_panel_closed()
            next_page_idx, next_selection_id = result
            try:
                return _select_block_outputs(
                    local_md_dir,
                    file_name,
                    next_page_idx,
                    next_selection_id,
                    request_locale,
                )
            except Exception:
                logger.exception(f"Editor suspect selection failed for {file_name}")
                return (
                    gr.skip(), gr.skip(),
                    render_save_status_html(
                        i18n,
                        "editor_load_error",
                        locale=request_locale,
                        detail=f" ({time.strftime('%H:%M:%S')})",
                        is_error=True,
                    ),
                    page_idx, None,
                ) + _editor_panel_closed()

        def _refresh_editor_tabs_after_edit(local_md_dir, file_name):
            """Перечитывает .md/content_list.json с диска после regenerate_derived_outputs."""
            md_path = Path(local_md_dir) / f"{file_name}.md"
            content_list_path = Path(local_md_dir) / f"{file_name}_content_list.json"
            md_text_value = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
            content_list_value = (
                content_list_path.read_text(encoding="utf-8") if content_list_path.is_file() else ""
            )
            rendered_md = prepare_markdown_for_gradio_preview(
                replace_image_with_gradio_file_urls(md_text_value, local_md_dir),
                latex_delimiters,
            )
            return rendered_md, md_text_value, content_list_value

        _editor_apply_outputs = [
            md,
            md_text,
            content_list_json,
            editor_image,
            editor_status_html,
            exports_dirty_state,
            editor_dirty_html,
        ]

        def editor_apply_text_edit(text_value, selected, local_md_dir, file_name, request: gr.Request = None):
            """Читает middle.json с диска заново, применяет правку, сохраняет, пересобирает
            .md/content_list*.json и обновляет вкладки Markdown/JSON текущего результата."""
            request_locale = resolve_request_locale(request)
            _skip7 = (gr.skip(),) * 4
            if not selected or selected.get("kind") != "text" or not local_md_dir or not file_name:
                return _skip7 + (
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )
            page_idx = selected["page_idx"]
            block_idx = selected["block_idx"]
            source = selected.get("source", "para_blocks")
            try:
                middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                visual_editor.apply_text_edit(
                    middle_json,
                    page_idx,
                    block_idx,
                    text_value or "",
                    source=source,
                )
                visual_editor.save_middle_json(local_md_dir, file_name, middle_json)
                regenerate_derived_outputs(local_md_dir, file_name)
            except Exception as exc:
                logger.warning(f"Failed to apply text edit for {file_name}: {exc}")
                return _skip7 + (
                    render_save_status_html(
                        i18n, "editor_save_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )

            rendered_md, md_text_value, content_list_value = _refresh_editor_tabs_after_edit(
                local_md_dir, file_name
            )
            try:
                blocks = visual_editor.list_page_blocks(middle_json, page_idx)
                page_size = visual_editor.get_page_size(middle_json, page_idx)
                base_img = visual_editor.render_page_base(local_md_dir, file_name, page_idx)
                overlay_img = visual_editor.draw_overlay(base_img, blocks, page_size, selected)
            except Exception as exc:
                logger.warning(f"Failed to redraw editor overlay after text edit: {exc}")
                overlay_img = gr.skip()

            timestamp = time.strftime("%H:%M:%S")
            return (
                gr.update(value=rendered_md),
                gr.update(value=md_text_value),
                gr.update(value=content_list_value),
                overlay_img,
                render_save_status_html(
                    i18n, "editor_save_success", locale=request_locale, detail=f" ({timestamp})"
                ),
                True,
                render_editor_dirty_html(i18n, True, request_locale),
            )

        def editor_apply_table_edit(harvested_html, selected, local_md_dir, file_name, request: gr.Request = None):
            """Съём отредактированного innerHTML происходит на клиенте через js= (см. click
            ниже); здесь harvested_html -- уже итоговый HTML из #mineru-table-editor."""
            request_locale = resolve_request_locale(request)
            _skip7 = (gr.skip(),) * 4
            if not selected or selected.get("kind") != "table" or not local_md_dir or not file_name:
                return _skip7 + (
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )
            page_idx = selected["page_idx"]
            block_idx = selected["block_idx"]
            try:
                middle_json = visual_editor.load_middle_json(local_md_dir, file_name)
                source = selected.get("source", "para_blocks")
                block = middle_json["pdf_info"][page_idx][source][block_idx]
                page_size = visual_editor.get_page_size(middle_json, page_idx)
                table_span = visual_editor.recover_table_span_from_model(
                    local_md_dir, file_name, page_idx, block, page_size
                )
                if not table_span:
                    raise ValueError("Table span not found in the selected block")
                new_html = visual_editor.apply_table_edit(table_span["html"], harvested_html or "")
                table_span["html"] = new_html
                visual_editor.save_middle_json(local_md_dir, file_name, middle_json)
                regenerate_derived_outputs(local_md_dir, file_name)
            except ValueError as exc:
                return _skip7 + (
                    render_save_status_html(
                        i18n, "editor_table_structure_error", locale=request_locale,
                        detail=f": {exc}", is_error=True,
                    ),
                    gr.skip(),
                    gr.skip(),
                )
            except Exception as exc:
                logger.warning(f"Failed to apply table edit for {file_name}: {exc}")
                return _skip7 + (
                    render_save_status_html(
                        i18n, "editor_save_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )

            rendered_md, md_text_value, content_list_value = _refresh_editor_tabs_after_edit(
                local_md_dir, file_name
            )
            try:
                blocks = visual_editor.list_page_blocks(middle_json, page_idx)
                page_size = visual_editor.get_page_size(middle_json, page_idx)
                base_img = visual_editor.render_page_base(local_md_dir, file_name, page_idx)
                overlay_img = visual_editor.draw_overlay(base_img, blocks, page_size, selected)
            except Exception as exc:
                logger.warning(f"Failed to redraw editor overlay after table edit: {exc}")
                overlay_img = gr.skip()

            timestamp = time.strftime("%H:%M:%S")
            return (
                gr.update(value=rendered_md),
                gr.update(value=md_text_value),
                gr.update(value=content_list_value),
                overlay_img,
                render_save_status_html(
                    i18n, "editor_save_success", locale=request_locale, detail=f" ({timestamp})"
                ),
                True,
                render_editor_dirty_html(i18n, True, request_locale),
            )

        def editor_apply_selected_edit(
            text_value,
            harvested_table_html,
            selected,
            local_md_dir,
            file_name,
            request: gr.Request = None,
        ):
            """Сохраняет текущий выбранный блок через единственную кнопку UI."""
            if selected and selected.get("kind") == "text":
                return editor_apply_text_edit(
                    text_value, selected, local_md_dir, file_name, request
                )
            if selected and selected.get("kind") == "table":
                return editor_apply_table_edit(
                    harvested_table_html, selected, local_md_dir, file_name, request
                )

            request_locale = resolve_request_locale(request)
            return (gr.skip(),) * 4 + (
                render_save_status_html(
                    i18n, "editor_no_result", locale=request_locale, is_error=True
                ),
                gr.skip(),
                gr.skip(),
            )

        async def editor_refresh_exports(local_md_dir, file_name, output_file_path, request: gr.Request = None):
            """Пересобирает DOCX/searchable-PDF из ТЕКУЩИХ файлов на диске (не вызывает
            regenerate_derived_outputs -- кнопка полезна и после правки content_list.json
            вручную) и пере-архивирует результат в тот же .zip, что скачивает пользователь."""
            request_locale = resolve_request_locale(request)
            _skip5 = (gr.skip(),) * 5
            if not local_md_dir or not file_name or not output_file_path:
                return _skip5 + (
                    render_save_status_html(
                        i18n, "editor_no_result", locale=request_locale, is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )
            docx_preview_pdf_value = gr.update(visible=False)
            docx_preview_html_value = gr.update(value="", visible=False)
            try:
                docx_path = await asyncio.to_thread(build_gradio_docx_result, local_md_dir, file_name)
                if docx_path:
                    preview_pdf = await asyncio.to_thread(convert_docx_to_pdf_for_preview, docx_path)
                    if preview_pdf:
                        docx_preview_pdf_value = gr.update(value=preview_pdf, visible=True)
                    else:
                        try:
                            docx_preview_html_value = gr.update(
                                value=build_office_preview_html(docx_path, request, i18n),
                                visible=True,
                            )
                        except Exception as exc:
                            logger.warning(f"Failed to build DOCX preview HTML for {docx_path}: {exc}")
                searchable_pdf_path = None
                if docx_path:
                    docx_file = Path(docx_path)
                    searchable_pdf_path = await asyncio.to_thread(
                        build_gradio_searchable_pdf_result, str(docx_file.parent), docx_file.stem
                    )
                zip_target = Path(output_file_path)
                zip_result = await asyncio.to_thread(
                    compress_directory_to_zip, local_md_dir, zip_target
                )
                if zip_result != 0:
                    raise RuntimeError("Failed to re-archive the result directory")
            except Exception as exc:
                logger.warning(f"Failed to refresh DOCX/searchable PDF exports for {file_name}: {exc}")
                return _skip5 + (
                    render_save_status_html(
                        i18n, "editor_refresh_error", locale=request_locale, detail=f": {exc}", is_error=True
                    ),
                    gr.skip(),
                    gr.skip(),
                )

            timestamp = time.strftime("%H:%M:%S")
            return (
                docx_path,
                searchable_pdf_path,
                docx_preview_pdf_value,
                docx_preview_html_value,
                str(zip_target),
                render_save_status_html(
                    i18n, "editor_refresh_success", locale=request_locale, detail=f" ({timestamp})"
                ),
                False,
                render_editor_dirty_html(i18n, False, request_locale),
            )

        editor_prev_bu.click(
            fn=editor_prev,
            inputs=[local_md_dir_state, file_name_state, editor_page_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        editor_next_bu.click(
            fn=editor_next,
            inputs=[local_md_dir_state, file_name_state, editor_page_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        editor_zoom_out_bu.click(
            fn=None,
            inputs=[],
            outputs=[],
            js="() => { window.mineruEditorZoomOut && window.mineruEditorZoomOut(); }",
        )
        editor_zoom_in_bu.click(
            fn=None,
            inputs=[],
            outputs=[],
            js="() => { window.mineruEditorZoomIn && window.mineruEditorZoomIn(); }",
        )
        editor_zoom_fit_bu.click(
            fn=None,
            inputs=[],
            outputs=[],
            js="() => { window.mineruEditorZoomFit && window.mineruEditorZoomFit(); }",
        )
        editor_zoom_actual_bu.click(
            fn=None,
            inputs=[],
            outputs=[],
            js="() => { window.mineruEditorZoomActual && window.mineruEditorZoomActual(); }",
        )
        editor_deselect_bu.click(
            fn=editor_deselect,
            inputs=[local_md_dir_state, file_name_state, editor_page_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        # Relay-канал кликов: JS пишет JSON в скрытый textbox и диспатчит
        # событие DOM "input". Привязка -- .change(), НЕ .input(): в
        # gradio@6.8.0 js/textbox/shared/Textbox.svelte GRADIO-уровневый
        # колбэк oninput?.(value) (тот, что в итоге вызывает Python-обработчик
        # .input()) вызывается ТОЛЬКО из handle_keypress -- обработчика
        # нативного события "keypress", т.е. только на реальное нажатие
        # клавиши. Наш JS никогда не шлёт keypress, только программно меняет
        # .value и диспатчит "input" -- это доходит до Svelte bind:value
        # (обновляет DOM/реактивное состояние), но НЕ доходит до oninput
        # Гradio. handle_change() (реактивный эффект на bind:value, который
        # срабатывает при ЛЮБОМ изменении значения независимо от источника)
        # вызывает onchange?.(value) -- источник Python .change(). Отсюда и
        # был симптом «relay визуально работал (клик распознавался, JSON
        # писался в textbox), но ни одного queue/join не уходило»: колбэк на
        # который был подписан Python, физически не мог быть вызван нашим
        # способом взаимодействия с полем.
        editor_click_relay_tb.change(
            fn=editor_image_coordinate_select,
            inputs=[editor_click_relay_tb, local_md_dir_state, file_name_state, editor_page_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        # Родное Gallery.select остаётся резервным путём: обработчик в
        # gradio_app.js глушит клик по миниатюре (capture) и шлёт его через
        # relay; сюда событие доходит только если relay-канал недоступен
        # (например, скрипт не загрузился) -- тогда работает штатный, пусть и
        # нестабильный в Gradio 6, путь.
        editor_thumbnails_gallery.select(
            fn=editor_thumbnail_select,
            inputs=[local_md_dir_state, file_name_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        editor_table_picker.change(
            fn=editor_table_picker_select,
            inputs=[editor_table_picker, local_md_dir_state, file_name_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        editor_next_suspect_bu.click(
            fn=editor_next_suspect,
            inputs=[local_md_dir_state, file_name_state, editor_page_state, selected_block_state],
            outputs=_editor_view_outputs,
            **_editor_view_api_kwargs,
        )
        editor_apply_bu.click(
            fn=editor_apply_selected_edit,
            inputs=[
                editor_block_textbox,
                editor_table_harvest_tb,
                selected_block_state,
                local_md_dir_state,
                file_name_state,
            ],
            outputs=_editor_apply_outputs,
            js=(
                "(text, table, sel, d, f) => [text, document.getElementById('mineru-table-editor')"
                "?.innerHTML ?? table ?? '', sel, d, f]"
            ),
            **_editor_view_api_kwargs,
        )
        editor_refresh_exports_bu.click(
            fn=editor_refresh_exports,
            inputs=[local_md_dir_state, file_name_state, output_file],
            outputs=[
                output_docx,
                output_searchable_pdf,
                docx_preview_pdf,
                docx_preview_html,
                output_file,
                editor_status_html,
                exports_dirty_state,
                editor_dirty_html,
            ],
            **_editor_view_api_kwargs,
        )

    demo.queue(default_concurrency_limit=None)

    if IS_GRADIO_6:
        footer_links = []
        client_translations = json.dumps(
            {
                "ru": i18n.translations.get("ru", {}),
                "uk": i18n.translations.get("uk", {}),
            },
            ensure_ascii=False,
        ).replace("</", "<\\/")
        localized_app_head = APP_HEAD.replace(
            "__MINERU_UI_TRANSLATIONS__",
            client_translations,
        )
        _launch_kwargs = {
            "footer_links": footer_links,
            "css": APP_CSS,
            "head": localized_app_head,
        }
    else:
        _launch_kwargs = {"show_api": api_enable}
    maybe_prepare_local_api_for_gradio_startup(
        api_url=api_url,
        enable_vlm_preload=enable_vlm_preload,
    )
    demo.launch(
        server_name=server_name,
        server_port=server_port,
        i18n=i18n,
        allowed_paths=build_gradio_allowed_paths(),
        **_launch_kwargs,
    )


if __name__ == '__main__':
    main()
