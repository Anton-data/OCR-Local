# Copyright (c) Opendatalab. All rights reserved.
"""Backend logic for the Gradio "visual editor" tab: a FineReader-style zone
overlay on top of the rendered page image, click-to-edit text/table blocks,
with edits written back to ``*_middle.json`` (the single master file) so that
``.md`` / ``content_list.json`` / ``content_list_v2.json`` can be regenerated
from it afterwards (see ``mineru.cli.client_side_output.regenerate_derived_outputs``).

This module is intentionally free of any Gradio import -- it only deals with
``middle.json`` dicts, PIL images and plain HTML strings, so it can be unit
tested and reused without spinning up the UI.

Coordinate system: ``middle.json`` line/block ``bbox`` values are expressed in
the same point-based, top-left-origin space as the page's own ``page_size``
(see ``mineru.utils.searchable_pdf`` module docstring for the numeric
verification of this fact) -- the same space PyMuPDF uses for ``page.rect``,
so converting to the rendered image's pixel space is a simple linear scale.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import fitz
from loguru import logger
from PIL import Image, ImageDraw

from mineru.utils.searchable_pdf import ORIGIN_PDF_SUFFIX

EDITOR_CACHE_DIRNAME = "_editor_cache_v2"

# Адаптивный рендер: длинная сторона результата не должна превышать это
# значение (иначе большие сканы дают многомегабайтные PNG и тормозят UI).
_BASE_RENDER_DPI = 300
_MAX_RENDER_DIMENSION_PX = 3100
_MIN_RENDER_DPI = 36

_TEXT_BLOCK_TYPES = {"text", "title", "header"}
_EDITOR_DISCARDED_TEXT_TYPES = {"header"}

_BLOCK_COLORS: dict[str, tuple[int, int, int]] = {
    "text": (37, 99, 235),  # blue
    "title": (147, 51, 234),  # purple
    "table": (22, 163, 74),  # green
    "image": (234, 88, 12),  # orange
    "interline_equation": (107, 114, 128),  # gray
}
_DEFAULT_BLOCK_COLOR = (107, 114, 128)
_OVERLAY_FILL_ALPHA = 40
_SELECTED_OUTLINE_COLOR = (220, 38, 38)  # red
_SELECTED_OUTLINE_WIDTH = 4
_OVERLAY_OUTLINE_WIDTH = 2

_LOW_CONFIDENCE_SCORE_THRESHOLD = 0.85
_LOW_CONFIDENCE_OUTLINE_COLOR = (245, 200, 30)  # тёплый жёлтый -- сигнальный цвет, отличается от всех цветов типов блоков и от красной рамки выделения
_LOW_CONFIDENCE_OUTLINE_WIDTH = 3

_HIT_TEST_NEAR_RADIUS_PX = 15.0


# --- middle.json load/save -------------------------------------------------

def _middle_json_path(parse_dir: str | Path, doc_stem: str) -> Path:
    return Path(parse_dir) / f"{doc_stem}_middle.json"


def load_middle_json(parse_dir: str | Path, doc_stem: str) -> dict[str, Any]:
    """Читает ``{doc_stem}_middle.json`` из ``parse_dir``. Это единственный
    master-файл, который правит визуальный редактор; ``.md``/``content_list*``
    -- производные, пересобираемые через ``regenerate_derived_outputs``."""
    path = _middle_json_path(parse_dir, doc_stem)
    if not path.is_file():
        raise FileNotFoundError(f"Middle json not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def save_middle_json(parse_dir: str | Path, doc_stem: str, data: dict[str, Any]) -> Path:
    """Пишет middle.json обратно на диск. При первом сохранении (когда рядом
    ещё нет ``.bak``) создаёт разовую резервную копию исходного файла --
    без undo/history, просто страховка на случай неудачной правки."""
    path = _middle_json_path(parse_dir, doc_stem)
    backup_path = path.with_name(f"{doc_stem}_middle.json.bak")
    if path.is_file() and not backup_path.is_file():
        shutil.copyfile(path, backup_path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    return path


# --- page rendering ----------------------------------------------------

def _page_to_pixmap(page: "fitz.Page", dpi: int) -> "fitz.Pixmap":
    """По образцу ``mineru.utils.searchable_pdf._page_to_pixmap``."""
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    return page.get_pixmap(matrix=matrix, alpha=False)


def render_page_base(parse_dir: str | Path, doc_stem: str, page_idx: int) -> "Image.Image":
    """Рендерит страницу ``page_idx`` из ``{doc_stem}_origin.pdf`` (гарантированно
    существует рядом с middle.json для PDF/сканов -- см. ``ORIGIN_PDF_SUFFIX``
    в ``searchable_pdf.py``) и кэширует результат на диск, чтобы повторные
    клики по той же странице не рендерили её заново."""
    parse_dir = Path(parse_dir)
    cache_dir = parse_dir / EDITOR_CACHE_DIRNAME
    cache_path = cache_dir / f"page_{page_idx}.png"
    if cache_path.is_file():
        with Image.open(cache_path) as cached:
            return cached.convert("RGB")

    origin_pdf_path = parse_dir / f"{doc_stem}{ORIGIN_PDF_SUFFIX}"
    if not origin_pdf_path.is_file():
        raise FileNotFoundError(
            f"Original PDF not found for visual editor: {origin_pdf_path}"
        )

    doc = fitz.open(str(origin_pdf_path))
    try:
        if page_idx < 0 or page_idx >= doc.page_count:
            raise IndexError(
                f"Page {page_idx} out of range (document has {doc.page_count} pages)"
            )
        page = doc[page_idx]
        dpi = _BASE_RENDER_DPI
        pix = _page_to_pixmap(page, dpi)
        longest_side = max(pix.width, pix.height)
        if longest_side > _MAX_RENDER_DIMENSION_PX:
            dpi = max(
                _MIN_RENDER_DPI,
                int(dpi * _MAX_RENDER_DIMENSION_PX / longest_side),
            )
            pix = _page_to_pixmap(page, dpi)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()

    cache_dir.mkdir(parents=True, exist_ok=True)
    image.save(cache_path, format="PNG")
    return image


_THUMBNAIL_MAX_DIM_PX = 200
_THUMBNAIL_DPI = 60


def render_thumbnail(parse_dir: str | Path, doc_stem: str, page_idx: int) -> "Image.Image":
    """Маленький превью-рендер страницы для ленты миниатюр -- отдельное
    кэширование от render_page_base (тот кэширует полноразмерный 300dpi PNG,
    здесь нужен быстрый лёгкий рендер для всех страниц документа разом)."""
    parse_dir = Path(parse_dir)
    cache_dir = parse_dir / EDITOR_CACHE_DIRNAME
    cache_path = cache_dir / f"thumb_{page_idx}.png"
    if cache_path.is_file():
        with Image.open(cache_path) as cached:
            return cached.convert("RGB")
    origin_pdf_path = parse_dir / f"{doc_stem}{ORIGIN_PDF_SUFFIX}"
    if not origin_pdf_path.is_file():
        raise FileNotFoundError(f"Original PDF not found for thumbnail: {origin_pdf_path}")
    doc = fitz.open(str(origin_pdf_path))
    try:
        if page_idx < 0 or page_idx >= doc.page_count:
            raise IndexError(f"Page {page_idx} out of range (document has {doc.page_count} pages)")
        page = doc[page_idx]
        pix = _page_to_pixmap(page, _THUMBNAIL_DPI)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        image.thumbnail((_THUMBNAIL_MAX_DIM_PX, _THUMBNAIL_MAX_DIM_PX), Image.LANCZOS)
    finally:
        doc.close()
    cache_dir.mkdir(parents=True, exist_ok=True)
    image.save(cache_path, format="PNG")
    return image


def list_thumbnails(parse_dir: str | Path, doc_stem: str) -> list["Image.Image"]:
    """Миниатюры всех страниц документа для ленты в UI редактора."""
    total = page_count(parse_dir, doc_stem)
    return [render_thumbnail(parse_dir, doc_stem, idx) for idx in range(total)]


def page_count(parse_dir: str | Path, doc_stem: str) -> int:
    """Возвращает число страниц в ``{doc_stem}_origin.pdf`` для навигации."""
    origin_pdf_path = Path(parse_dir) / f"{doc_stem}{ORIGIN_PDF_SUFFIX}"
    if not origin_pdf_path.is_file():
        return 0
    doc = fitz.open(str(origin_pdf_path))
    try:
        return doc.page_count
    finally:
        doc.close()


def has_origin_pdf(parse_dir: str | Path, doc_stem: str) -> bool:
    return (Path(parse_dir) / f"{doc_stem}{ORIGIN_PDF_SUFFIX}").is_file()


# --- block listing / geometry -------------------------------------------

def _block_min_score(block: dict[str, Any]) -> float | None:
    """Минимальный score среди всех span'ов блока; None, если ни у одного
    span'а нет score (бэкенд его не отдаёт -- тогда блок не подсвечивается,
    а не считается "неуверенным")."""
    scores: list[float] = []
    for line in block.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if isinstance(span, dict) and isinstance(span.get("score"), (int, float)):
                scores.append(float(span["score"]))
    return min(scores) if scores else None


def list_page_blocks(middle_json: dict[str, Any], page_idx: int) -> list[dict[str, Any]]:
    """Список редактируемых блоков страницы из ``para_blocks`` (НЕ
    ``preproc_blocks`` -- их читают ``union_make`` и searchable-PDF).
    ``discarded_blocks`` намеренно не включаются: они исключены из всех
    выходов, редактировать их бессмысленно."""
    pages = middle_json.get("pdf_info") or []
    if not isinstance(pages, list) or page_idx < 0 or page_idx >= len(pages):
        return []
    page = pages[page_idx]
    if not isinstance(page, dict):
        return []
    result = []
    sources = (
        ("para_blocks", page.get("para_blocks") or []),
        ("discarded_blocks", page.get("discarded_blocks") or []),
    )
    for source, blocks in sources:
        if not isinstance(blocks, list):
            continue
        for idx, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            raw_type = block.get("type")
            if source == "discarded_blocks" and raw_type not in _EDITOR_DISCARDED_TEXT_TYPES:
                continue
            if raw_type == "table":
                kind = "table"
            elif raw_type == "image":
                kind = "image"
            elif raw_type in _TEXT_BLOCK_TYPES:
                kind = "text"
            else:
                kind = "other"
            result.append(
                {
                    "block_idx": idx,
                    "source": source,
                    "selection_id": f"{source}:{idx}",
                    "kind": kind,
                    "bbox": block.get("bbox"),
                    "type": raw_type,
                    "min_score": _block_min_score(block),
                }
            )
    return result


def get_page_size(middle_json: dict[str, Any], page_idx: int) -> tuple[float, float]:
    """Возвращает ``page_size`` страницы (та же система координат, что и
    bbox блоков) -- нужно для пересчёта bbox в пиксели картинки."""
    pages = middle_json.get("pdf_info") or []
    if page_idx < 0 or page_idx >= len(pages):
        raise IndexError(f"Page index out of range: {page_idx}")
    page = pages[page_idx]
    size = page.get("page_size") if isinstance(page, dict) else None
    if not size or len(size) != 2:
        raise ValueError(f"Missing page_size for page {page_idx}")
    return float(size[0]), float(size[1])


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _bbox_to_pixels(
    bbox: list[float] | tuple[float, ...],
    scale_x: float,
    scale_y: float,
    img_w: int,
    img_h: int,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
    return (
        _clamp(x0 * scale_x, 0, img_w),
        _clamp(y0 * scale_y, 0, img_h),
        _clamp(x1 * scale_x, 0, img_w),
        _clamp(y1 * scale_y, 0, img_h),
    )


def _page_scale(page_size: tuple[float, float], img_size: tuple[int, int]) -> tuple[float, float]:
    page_w, page_h = page_size
    img_w, img_h = img_size
    scale_x = img_w / page_w if page_w else 1.0
    scale_y = img_h / page_h if page_h else 1.0
    return scale_x, scale_y


def draw_overlay(
    base_img: "Image.Image",
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    selected: dict[str, Any] | int | None = None,
) -> "Image.Image":
    """Копия ``base_img`` с полупрозрачными зонами по типу блока и красной
    рамкой на выбранном блоке (``selected_idx``, без заливки)."""
    base = base_img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    scale_x, scale_y = _page_scale(page_size, base.size)

    selected_idx = selected.get("block_idx") if isinstance(selected, dict) else selected
    selected_source = selected.get("source", "para_blocks") if isinstance(selected, dict) else "para_blocks"

    for block in blocks:
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = _bbox_to_pixels(bbox, scale_x, scale_y, base.width, base.height)
        if (
            selected_idx is not None
            and block.get("block_idx") == selected_idx
            and block.get("source", "para_blocks") == selected_source
        ):
            draw.rectangle(
                [x0, y0, x1, y1],
                outline=_SELECTED_OUTLINE_COLOR + (255,),
                width=_SELECTED_OUTLINE_WIDTH,
            )
            continue
        color = _BLOCK_COLORS.get(block.get("type"), _DEFAULT_BLOCK_COLOR)
        draw.rectangle(
            [x0, y0, x1, y1],
            fill=color + (_OVERLAY_FILL_ALPHA,),
            outline=color + (255,),
            width=_OVERLAY_OUTLINE_WIDTH,
        )
        min_score = block.get("min_score")
        if min_score is not None and min_score < _LOW_CONFIDENCE_SCORE_THRESHOLD:
            draw.rectangle(
                [x0, y0, x1, y1],
                outline=_LOW_CONFIDENCE_OUTLINE_COLOR + (255,),
                width=_LOW_CONFIDENCE_OUTLINE_WIDTH,
            )

    return Image.alpha_composite(base, overlay).convert("RGB")


def hit_test(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    img_size: tuple[int, int],
    x: float,
    y: float,
) -> str | None:
    """``x``/``y`` -- координаты клика в пикселях картинки (то, что реально
    отдаёт Gradio ``gr.Image.select()`` в ``SelectData.index``). При
    перекрытии bbox выбирается наименьший по площади; если ни один bbox не
    содержит точку -- берётся ближайший центр в радиусе 15px, иначе None."""
    scale_x, scale_y = _page_scale(page_size, img_size)
    img_w, img_h = img_size

    containing: list[tuple[float, int]] = []
    for block in blocks:
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = _bbox_to_pixels(bbox, scale_x, scale_y, img_w, img_h)
        if x0 <= x <= x1 and y0 <= y <= y1:
            area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
            containing.append((area, block["selection_id"]))
    if containing:
        containing.sort(key=lambda item: item[0])
        return containing[0][1]

    best_idx: str | None = None
    best_dist = _HIT_TEST_NEAR_RADIUS_PX
    for block in blocks:
        bbox = block.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = _bbox_to_pixels(bbox, scale_x, scale_y, img_w, img_h)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        dist = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_idx = block["selection_id"]
    return best_idx


def find_next_suspect_block(
    middle_json: dict[str, Any],
    start_page_idx: int,
    start_block_idx: int | None = None,
    threshold: float = _LOW_CONFIDENCE_SCORE_THRESHOLD,
) -> tuple[int, int] | None:
    """Ищет следующий блок с min_score < threshold, начиная со start_page_idx
    (на этой странице -- только блоки С block_idx БОЛЬШЕ start_block_idx, если
    он задан), дальше по всем страницам по кругу. Если после полного круга
    ничего не найдено, но был start_block_idx (т.е. первый проход стартовой
    страницы был сужен), делается финальный проход по стартовой странице без
    сужения -- иначе блоки с block_idx <= start_block_idx на ней никогда не
    проверяются. None, если подозрительных блоков нет вовсе."""
    pages = middle_json.get("pdf_info") or []
    total_pages = len(pages)
    if total_pages == 0:
        return None
    for offset in range(total_pages):
        page_idx = (start_page_idx + offset) % total_pages
        blocks = list_page_blocks(middle_json, page_idx)
        min_idx = start_block_idx if (page_idx == start_page_idx and start_block_idx is not None) else -1
        candidates = [
            b for b in blocks
            if b["block_idx"] > min_idx and b.get("min_score") is not None and b["min_score"] < threshold
        ]
        if candidates:
            candidates.sort(key=lambda b: b["block_idx"])
            return page_idx, candidates[0]["block_idx"]

    if start_block_idx is not None:
        blocks = list_page_blocks(middle_json, start_page_idx)
        candidates = [
            b for b in blocks
            if b["block_idx"] <= start_block_idx and b.get("min_score") is not None and b["min_score"] < threshold
        ]
        if candidates:
            candidates.sort(key=lambda b: b["block_idx"])
            return start_page_idx, candidates[0]["block_idx"]

    return None


def get_block_snippet(
    base_img: "Image.Image",
    page_size: tuple[float, float],
    bbox: list[float] | tuple[float, ...],
    padding_px: int = 8,
    min_width_px: int = 400,
    upscale_factor: int = 2,
) -> "Image.Image":
    """Кроп base_img по bbox блока (те же координаты, что использует draw_overlay/
    hit_test) с полями и апскейлом мелких блоков -- для показа рядом с полем
    правки (сверка оригинала и распознанного текста, как в ABBYY FineReader)."""
    scale_x, scale_y = _page_scale(page_size, base_img.size)
    x0, y0, x1, y1 = _bbox_to_pixels(bbox, scale_x, scale_y, base_img.width, base_img.height)
    x0 = max(0, int(x0) - padding_px)
    y0 = max(0, int(y0) - padding_px)
    x1 = min(base_img.width, int(x1) + padding_px)
    y1 = min(base_img.height, int(y1) + padding_px)
    crop = base_img.crop((x0, y0, x1, y1))
    if crop.width and crop.width < min_width_px:
        crop = crop.resize(
            (crop.width * upscale_factor, crop.height * upscale_factor), Image.LANCZOS
        )
    return crop


# --- text block editing --------------------------------------------------

def get_block_text(block: dict[str, Any]) -> str:
    """Строки блока (``block["lines"]``) через ``"\\n"``; спаны внутри строки
    (``line["spans"]``, поле ``content``) конкатенируются."""
    lines = block.get("lines") or []
    line_texts = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        spans = line.get("spans") or []
        parts = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            content = span.get("content")
            if content:
                parts.append(str(content))
        line_texts.append("".join(parts))
    return "\n".join(line_texts)


def apply_text_edit(
    middle_json: dict[str, Any],
    page_idx: int,
    block_idx: int,
    new_text: str,
    source: str = "para_blocks",
) -> None:
    """Мутирует ``middle_json["pdf_info"][page_idx]["para_blocks"][block_idx]``
    на месте. Если число строк в правке совпало с исходным -- геометрия
    построчная сохраняется (правка "по месту"); иначе весь текст схлопывается
    в один line с bbox блока целиком.

    Осознанное упрощение: при схлопывании теряется внутриблочная построчная
    геометрия ТОЛЬКО для отредактированного блока (bbox блока, type, angle --
    не трогаются). searchable-PDF (``searchable_pdf.py``) имеет ink-guided
    placement для текста без чёткой построчной разбивки -- она раскладывает
    слова по видимым чернильным полосам скана, так что итоговый текстовый
    слой всё равно останется читаемым.
    """
    pages = middle_json.get("pdf_info") or []
    if page_idx < 0 or page_idx >= len(pages):
        raise IndexError(f"Page index out of range: {page_idx}")
    if source not in {"para_blocks", "discarded_blocks"}:
        raise ValueError(f"Unsupported block source: {source}")
    blocks = pages[page_idx].get(source) or []
    if block_idx < 0 or block_idx >= len(blocks):
        raise IndexError(f"Block index out of range: {block_idx}")

    block = blocks[block_idx]
    orig_lines = block.get("lines") or []
    new_lines = (new_text or "").split("\n")
    while new_lines and new_lines[-1] == "":
        new_lines.pop()

    if orig_lines and len(new_lines) == len(orig_lines):
        for line, text in zip(orig_lines, new_lines):
            line["spans"] = [{"bbox": line.get("bbox"), "type": "text", "content": text}]
    else:
        block["lines"] = [
            {
                "bbox": block.get("bbox"),
                "spans": [
                    {
                        "bbox": block.get("bbox"),
                        "type": "text",
                        "content": "\n".join(new_lines),
                    }
                ],
            }
        ]


# --- table block editing ---------------------------------------------------

def find_table_span(block: dict[str, Any]) -> dict[str, Any] | None:
    """Ищет первый span с ``type == "table"`` и полем ``html`` в поддереве
    блока. Структурно-агностично к вложенности (MinerU кладёт HTML таблицы в
    ``block["blocks"][i]["lines"][j]["spans"][k]``, но обход рекурсивный, а не
    завязан на конкретную глубину)."""
    lines = block.get("lines") or []
    for line in lines:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if isinstance(span, dict) and span.get("type") == "table" and span.get("html"):
                return span
    for sub_block in block.get("blocks") or []:
        if isinstance(sub_block, dict):
            found = find_table_span(sub_block)
            if found is not None:
                return found
    return None


class _EditableTableBuilder(HTMLParser):
    """Копирует table_html 1:1, добавляя ``contenteditable="true"`` каждому
    ``<td>``/``<th>`` и вырезая любые ``<script>`` (гигиена перед вставкой в
    браузер)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_script = 0

    @staticmethod
    def _format_attrs(attrs: list[tuple[str, str | None]], extra: str | None = None) -> str:
        parts = []
        for name, value in attrs:
            if value is None:
                parts.append(name)
            else:
                parts.append(f'{name}="{html.escape(value, quote=True)}"')
        if extra:
            parts.append(extra)
        return (" " + " ".join(parts)) if parts else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._skip_script += 1
            return
        if self._skip_script:
            return
        extra = 'contenteditable="true"' if tag in ("td", "th") else None
        self._out.append(f"<{tag}{self._format_attrs(attrs, extra)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" or self._skip_script:
            return
        self._out.append(f"<{tag}{self._format_attrs(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._skip_script:
                self._skip_script -= 1
            return
        if self._skip_script:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_script:
            return
        self._out.append(html.escape(data))

    def get_html(self) -> str:
        return "".join(self._out)


_TABLE_EDITOR_HOST_ID = "mineru-table-editor"


def make_editable_table_html(table_html: str) -> str:
    """Оборачивает ``table_html`` (уже включает свой ``<table>``) в
    ``<div id="mineru-table-editor">`` с ``contenteditable`` на каждой ячейке
    и минимальным инлайн-CSS (видимые границы ячеек, обводка при фокусе)."""
    builder = _EditableTableBuilder()
    builder.feed(table_html or "")
    inner = builder.get_html()
    return (
        f'<div id="{_TABLE_EDITOR_HOST_ID}" style="overflow:auto;">'
        "<style>"
        f"#{_TABLE_EDITOR_HOST_ID} table{{border-collapse:collapse;width:100%;}}"
        f"#{_TABLE_EDITOR_HOST_ID} td,#{_TABLE_EDITOR_HOST_ID} th{{"
        "border:1px solid #999;padding:4px 6px;min-width:24px;}"
        f"#{_TABLE_EDITOR_HOST_ID} td:focus,#{_TABLE_EDITOR_HOST_ID} th:focus{{"
        "outline:2px solid #2563eb;background:rgba(37,99,235,0.08);}"
        "</style>"
        f"{inner}"
        "</div>"
    )


class _CellTextExtractor(HTMLParser):
    """Извлекает по ячейкам (``td``/``th``) плоский текст: вложенные теги
    (``<div>``, ``<br>`` и т.п., которые contenteditable вставляет по Enter)
    трактуются как разделитель слов, а не как разметка."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self._depth = 0
        self._buf: list[str] = []
        self._skip_script = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._skip_script += 1
            return
        if self._skip_script:
            return
        if tag in ("td", "th"):
            self._depth += 1
            if self._depth == 1:
                self._buf = []
        elif self._depth:
            self._buf.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" or self._skip_script:
            return
        if self._depth:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._skip_script:
                self._skip_script -= 1
            return
        if self._skip_script:
            return
        if tag in ("td", "th") and self._depth:
            self._depth -= 1
            if self._depth == 0:
                text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                self.texts.append(text)
        elif self._depth:
            self._buf.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_script:
            return
        if self._depth:
            self._buf.append(data)


def _extract_cell_texts(table_html: str) -> list[str]:
    parser = _CellTextExtractor()
    parser.feed(table_html or "")
    parser.close()
    return parser.texts


class _CellReplacer(HTMLParser):
    """Пересобирает HTML оригинальной структуры таблицы, заменяя текстовое
    содержимое каждой ``td``/``th`` новым текстом (из браузерной версии) по
    порядку обхода документа. Все теги/атрибуты (``colspan``/``rowspan`` и
    порядок) берутся из оригинала -- так безопаснее, чем доверять разметке,
    которую браузер мог исказить при contenteditable-редактировании."""

    def __init__(self, replacement_texts: list[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._texts = replacement_texts
        self._next_idx = 0
        self._depth = 0
        self._skip_script = 0

    @staticmethod
    def _format_starttag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = [tag]
        for name, value in attrs:
            if value is None:
                parts.append(name)
            else:
                parts.append(f'{name}="{html.escape(value, quote=True)}"')
        return "<" + " ".join(parts) + ">"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._skip_script += 1
            return
        if self._skip_script:
            return
        self._out.append(self._format_starttag(tag, attrs))
        if tag in ("td", "th"):
            self._depth += 1
            if self._depth == 1:
                text = self._texts[self._next_idx] if self._next_idx < len(self._texts) else ""
                self._next_idx += 1
                self._out.append(html.escape(text))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" or self._skip_script:
            return
        self._out.append(self._format_starttag(tag, attrs)[:-1] + " />")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._skip_script:
                self._skip_script -= 1
            return
        if self._skip_script:
            return
        if tag in ("td", "th") and self._depth:
            self._depth -= 1
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_script:
            return
        if self._depth:
            return  # содержимое ячейки уже подставлено в handle_starttag
        self._out.append(html.escape(data))

    def get_html(self) -> str:
        return "".join(self._out)


def apply_table_edit(orig_html: str, browser_html: str) -> str:
    """Возвращает HTML со структурой ``orig_html`` и текстами ячеек из
    ``browser_html`` (после contenteditable-правки в браузере). Бросает
    ``ValueError``, если число ячеек (``td``/``th``) не совпало -- через
    contenteditable пользователь не мог легально поменять структуру таблицы,
    только текст внутри ячеек."""
    orig_cell_count = len(_extract_cell_texts(orig_html))
    new_texts = _extract_cell_texts(browser_html)
    if orig_cell_count != len(new_texts):
        raise ValueError(
            f"Table structure changed: expected {orig_cell_count} cells, "
            f"got {len(new_texts)}. Only cell text can be edited, not structure."
        )
    replacer = _CellReplacer(new_texts)
    replacer.feed(orig_html or "")
    replacer.close()
    return replacer.get_html()
