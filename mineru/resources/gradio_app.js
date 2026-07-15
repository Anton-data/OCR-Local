() => {
    const POPOVER_SCRIPT_VERSION = "locale-switch-v1";
    if (window.__mineruAdvancedPopoverInstalled === POPOVER_SCRIPT_VERSION) {
        return;
    }
    window.__mineruAdvancedPopoverInstalled = POPOVER_SCRIPT_VERSION;

    const POPOVER_OPEN_CLASS = "mineru-advanced-popover-open";
    const CLIENT_OPTIONS_VISIBLE_CLASS = "mineru-show-client-options";
    const IMAGE_ANALYSIS_VISIBLE_CLASS = "mineru-show-image-analysis";
    const OCR_LANGUAGE_VISIBLE_CLASS = "mineru-show-ocr-language";
    const FORCE_OCR_HIDDEN_CLASS = "mineru-hide-force-ocr";
    const HYBRID_EFFORT_HIDDEN_CLASS = "mineru-hide-hybrid-effort";
    const UI_LOCALE_STORAGE_KEY = "mineru-ui-locale";
    const OFFICE_PREVIEW_NOTICE_STORAGE_KEY = "mineru.officePreviewNoticeIgnored";
    const OPEN_DELAY_MS = 120;
    const CLOSE_DELAY_MS = 280;
    const ANIMATION_DELAY_MS = 140;
    const CLIPBOARD_MIME_EXTENSIONS = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
        "image/tiff": "tiff",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    };
    // 俄语和乌克兰语分别保留；其他语言仍按现有回退规则处理。
    const normalizeMineruLocale = (locale) => {
        const normalized = String(locale || "").toLowerCase();
        if (normalized.startsWith("zh")) {
            return "zh";
        }
        if (normalized.startsWith("ru")) {
            return "ru";
        }
        if (normalized.startsWith("uk")) {
            return "uk";
        }
        return "en";
    };

    // 启动脚本会把用户选择写入 navigator.language，Gradio 与自定义 HTML 使用同一语言。
    const resolveMineruLocale = () => {
        if (typeof navigator !== "undefined") {
            const languages = Array.from(navigator.languages || []);
            const primaryLocale = languages[0] || navigator.language;
            if (primaryLocale) {
                return normalizeMineruLocale(primaryLocale);
            }
        }
        return normalizeMineruLocale(document.documentElement.getAttribute("lang"));
    };

    // Gradio 只会自动翻译组件属性；header/status 这类自定义 HTML 需要前端按浏览器语言补一次。
    // 优先使用当前语言文案，找不到时依次降级到俄语、英语，保证始终有可显示的文本。
    const localizeMineruCustomText = () => {
        const locale = resolveMineruLocale();
        document.querySelectorAll("[data-mineru-i18n-key]").forEach((item) => {
            const localizedText = item.getAttribute(`data-mineru-i18n-${locale}`)
                || item.getAttribute("data-mineru-i18n-ru")
                || item.getAttribute("data-mineru-i18n-en");
            if (localizedText !== null && item.textContent !== localizedText) {
                item.textContent = localizedText;
            }
        });
    };

    const applyLanguageSwitchState = () => {
        const locale = resolveMineruLocale();
        document.querySelectorAll(".mineru-language-option").forEach((button) => {
            const isActive = button.getAttribute("data-mineru-locale") === locale;
            button.classList.toggle("is-active", isActive);
            button.setAttribute("aria-pressed", String(isActive));
        });
    };

    const normalizeUiText = (value) => String(value || "").trim().replace(/\s+/g, " ");

    // Gradio 6.8 does not ship a Ukrainian core locale. Translate only interface
    // containers, never document/Markdown output, so recognized content stays untouched.
    const localizeGradioInterface = () => {
        const locale = resolveMineruLocale();
        const dictionaries = window.__mineruUiTranslations || {};
        const russian = dictionaries.ru || {};
        const target = dictionaries[locale] || russian;
        const translationMap = new Map();

        if (locale === "uk") {
            Object.keys(russian).forEach((key) => {
                if (typeof russian[key] === "string" && typeof target[key] === "string") {
                    translationMap.set(normalizeUiText(russian[key]), target[key]);
                }
            });
        }

        const builtinTranslations = locale === "uk"
            ? {
                "Перетащите файл сюда": "Перетягніть файл сюди",
                "- или -": "- або -",
                "Нажмите для загрузки": "Натисніть для завантаження",
                "Использовать через API": "Використовувати через API",
                "Создано с помощью Gradio": "Створено за допомогою Gradio",
                "Настройки": "Налаштування",
                "Логотип": "Логотип",
                "Reset to default value": "Скинути до типового значення",
                "Click to upload or drop files": "Натисніть або перетягніть файл для завантаження",
                "Copy conversation": "Копіювати",
                "doc preview": "Попередній перегляд документа",
                "Empty value": "Порожньо",
                "Stop Recording": "Зупинити запис",
            }
            : {
                "Reset to default value": "Сбросить значение",
                "Click to upload or drop files": "Нажмите или перетащите файл для загрузки",
                "Copy conversation": "Копировать",
                "doc preview": "Предпросмотр документа",
                "Empty value": "Пусто",
                "Stop Recording": "Остановить запись",
            };
        Object.entries(builtinTranslations).forEach(([source, translated]) => {
            translationMap.set(normalizeUiText(source), translated);
        });

        const translateValue = (value) => translationMap.get(normalizeUiText(value));
        const translateElement = (element) => {
            Array.from(element.childNodes || []).forEach((node) => {
                if (node.nodeType !== Node.TEXT_NODE || !node.textContent?.trim()) {
                    return;
                }
                const translated = translateValue(node.textContent);
                if (!translated) {
                    return;
                }
                const leading = node.textContent.match(/^\s*/)?.[0] || "";
                const trailing = node.textContent.match(/\s*$/)?.[0] || "";
                node.textContent = `${leading}${translated}${trailing}`;
            });
            ["aria-label", "title", "placeholder", "alt"].forEach((attribute) => {
                const currentValue = element.getAttribute?.(attribute);
                let translated = currentValue && translateValue(currentValue);
                if (currentValue && !translated) {
                    for (const [source, replacement] of translationMap.entries()) {
                        if (source && currentValue.includes(source)) {
                            translated = currentValue.replace(source, replacement);
                            break;
                        }
                    }
                }
                if (translated && translated !== currentValue) {
                    element.setAttribute(attribute, translated);
                }
            });
        };

        const roots = document.querySelectorAll(
            ".mineru-header-html, .mineru-control-column, .mineru-advanced-popover, footer"
        );
        roots.forEach((root) => {
            translateElement(root);
            root.querySelectorAll("*").forEach(translateElement);
        });
        document.querySelectorAll(
            ".mineru-markdown-tabs button, .mineru-markdown-tabs [role='tab'], "
            + ".mineru-preview-pane label, .mineru-preview-pane [data-testid='block-label'], "
            + ".mineru-preview-pane [aria-label='Empty value'], "
            + ".mineru-header-html img"
        ).forEach(translateElement);
    };

    const selectUiLocale = (locale) => {
        if (locale !== "ru" && locale !== "uk") {
            return;
        }
        try {
            localStorage.setItem(UI_LOCALE_STORAGE_KEY, locale);
        } catch (_error) {
            // Query parameter below still makes the switch work without localStorage.
        }
        document.cookie = `${UI_LOCALE_STORAGE_KEY}=${locale}; Path=/; Max-Age=31536000; SameSite=Lax`;
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set("lang", locale);
        window.location.assign(nextUrl.toString());
    };

    // 读取浏览器本地偏好时做容错，避免隐私模式禁用 localStorage 影响页面初始化。
    const getOfficePreviewNoticeIgnored = () => {
        try {
            return localStorage.getItem(OFFICE_PREVIEW_NOTICE_STORAGE_KEY) === "1";
        } catch (error) {
            return false;
        }
    };

    // 保存“不再提示”偏好；失败时仅降级为本次点击隐藏，不阻断预览。
    const setOfficePreviewNoticeIgnored = () => {
        try {
            localStorage.setItem(OFFICE_PREVIEW_NOTICE_STORAGE_KEY, "1");
        } catch (error) {
            return false;
        }
        return true;
    };

    const findOfficePreviewNotices = () =>
        document.querySelectorAll(".office-preview-notice");

    // 根据浏览器持久偏好隐藏新挂载的 Office 预览提示。
    const applyOfficePreviewNoticePreference = () => {
        if (!getOfficePreviewNoticeIgnored()) {
            return;
        }
        findOfficePreviewNotices().forEach((notice) => {
            notice.classList.add("is-dismissed");
        });
    };

    // 自定义 HTML 由 Gradio 动态重绘，统一在 DOM 变更后补本地化和忽略状态。
    const refreshMineruCustomHtml = () => {
        localizeMineruCustomText();
        localizeGradioInterface();
        applyLanguageSwitchState();
        applyOfficePreviewNoticePreference();
        refreshMineruOptionVisibility();
    };

    // 兼容 Gradio 将 elem_classes 挂到按钮自身或按钮外层容器的两种 DOM 结构。
    const findButton = () => document.querySelector(
        "button.mineru-advanced-open, .mineru-advanced-open button, .mineru-advanced-open"
    );
    const findPopover = () => document.querySelector(".mineru-advanced-popover");
    const findBackendRoot = () => document.querySelector(".mineru-backend-select");
    const findEffortRoot = () => document.querySelector(".mineru-hybrid-effort");
    let openTimer = null;
    let closeTimer = null;
    let visibilityTimer = null;
    let hoverHandlersInstalled = false;

    // 读取 Gradio Dropdown 当前值；value 属性比可见文本更稳定，避免中英文文案影响判断。
    const getBackendValue = () => {
        const backendRoot = findBackendRoot();
        const backendControl = backendRoot?.querySelector('[role="listbox"]');
        return (backendControl?.value || backendControl?.textContent || "").trim();
    };

    // 读取 Hybrid effort 当前值；控件在非 hybrid 后端会被 Gradio 隐藏，缺失时按空值处理。
    const getEffortValue = () => {
        const effortRoot = findEffortRoot();
        const checkedRadio = effortRoot?.querySelector(
            'input[type="radio"]:checked, input[type="radio"][aria-checked="true"]'
        );
        return (checkedRadio?.value || "").trim();
    };

    // 根据当前 backend/effort 刷新前端状态类，避免依赖 Gradio 重新挂载隐藏组件。
    const refreshMineruOptionVisibility = () => {
        const backend = getBackendValue();
        const effort = getEffortValue();
        const showClientOptions = backend.endsWith("http-client");
        const showImageAnalysis = backend.startsWith("vlm")
            || (backend.startsWith("hybrid") && effort === "high");
        const showOcrLanguage = backend === "pipeline";
        const hideForceOcr = backend !== "pipeline" && !backend.startsWith("hybrid");
        const hideHybridEffort = !backend.startsWith("hybrid");

        document.body.classList.toggle(CLIENT_OPTIONS_VISIBLE_CLASS, showClientOptions);
        document.body.classList.toggle(IMAGE_ANALYSIS_VISIBLE_CLASS, showImageAnalysis);
        document.body.classList.toggle(OCR_LANGUAGE_VISIBLE_CLASS, showOcrLanguage);
        document.body.classList.toggle(FORCE_OCR_HIDDEN_CLASS, hideForceOcr);
        document.body.classList.toggle(HYBRID_EFFORT_HIDDEN_CLASS, hideHybridEffort);
        if (document.body.classList.contains(POPOVER_OPEN_CLASS)) {
            positionPopover();
        }
    };

    // Gradio 控件会异步写回 value，延后一帧再读可以覆盖 Dropdown option 点击和 Radio 切换。
    const queueMineruOptionVisibilityRefresh = () => {
        requestAnimationFrame(() => {
            refreshMineruOptionVisibility();
            requestAnimationFrame(refreshMineruOptionVisibility);
        });
    };
    const findUploadFileInput = () => {
        const uploadRoot = document.querySelector(".mineru-upload-file");
        if (!uploadRoot) {
            return null;
        }
        return uploadRoot.querySelector('input[type="file"]');
    };

    // 读取上传控件 accept 规则，后续粘贴文件仍复用 gr.File 的支持格式边界。
    const getUploadAcceptedTypes = (uploadInput) => {
        const accept = uploadInput?.getAttribute("accept") || "";
        return accept.split(",").map((item) => item.trim().toLowerCase()).filter(Boolean);
    };

    // 判断剪贴板文件是否匹配 gr.File 当前支持的扩展名或 MIME 类型。
    const fileMatchesAcceptedType = (file, acceptedTypes) => {
        if (!acceptedTypes.length) {
            return true;
        }
        const name = (file.name || "").toLowerCase();
        const type = (file.type || "").toLowerCase();
        return acceptedTypes.some((accepted) => {
            if (accepted.startsWith(".")) {
                return name.endsWith(accepted);
            }
            if (accepted.endsWith("/*")) {
                return type.startsWith(accepted.slice(0, -1));
            }
            return type === accepted;
        });
    };

    // 为截图等无文件名剪贴板图片补一个扩展名，确保后端按普通图片文件解析。
    const buildClipboardFileName = (file) => {
        const type = (file.type || "").toLowerCase();
        const extension = CLIPBOARD_MIME_EXTENSIONS[type];
        if (!extension) {
            return "";
        }
        const timestamp = new Date().toISOString()
            .replace(/[-:]/g, "")
            .replace(/[.].+/, "")
            .replace("T", "-");
        const prefix = type.startsWith("image/") ? "clipboard-image" : "clipboard-file";
        return `${prefix}-${timestamp}.${extension}`;
    };

    // 保留浏览器暴露的原始文件；仅在文件名缺少扩展名时复制一份并补齐名称。
    const normalizeClipboardFile = (file) => {
        if (/[.][^.]+$/.test(file.name || "")) {
            return file;
        }
        const fileName = buildClipboardFileName(file);
        if (!fileName || typeof File === "undefined") {
            return file;
        }
        return new File([file], fileName, {
            type: file.type,
            lastModified: file.lastModified || Date.now(),
        });
    };

    // 同时兼容剪贴板 files 与 items，两种入口在不同浏览器里暴露情况不一致。
    const collectClipboardFiles = (clipboardData) => {
        const files = Array.from(clipboardData.files || []);
        if (files.length) {
            return files;
        }
        return Array.from(clipboardData.items || [])
            .filter((item) => item.kind === "file")
            .map((item) => item.getAsFile())
            .filter(Boolean);
    };

    // 构造只包含目标文件的 FileList；部分浏览器不允许构造 DataTransfer，需要降级处理。
    const createUploadFileList = (file) => {
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            return transfer.files;
        } catch (error) {
            return null;
        }
    };

    // 把文件列表赋值给 gr.File 的原生 input，并触发 Gradio 监听的变更事件。
    const assignClipboardFileToUpload = (uploadInput, uploadFiles) => {
        if (!uploadFiles) {
            return false;
        }
        try {
            uploadInput.files = uploadFiles;
        } catch (error) {
            return false;
        }
        uploadInput.dispatchEvent(new Event("input", { bubbles: true }));
        uploadInput.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
    };

    // 将剪贴板文件注入现有 gr.File input，避免为图片、PDF、Office 维护第二套上传链路。
    const uploadClipboardFile = (event) => {
        const clipboardData = event.clipboardData;
        const uploadInput = findUploadFileInput();
        if (!clipboardData || !uploadInput) {
            return false;
        }

        const acceptedTypes = getUploadAcceptedTypes(uploadInput);
        const rawClipboardFiles = clipboardData.files || null;
        const clipboardFiles = collectClipboardFiles(clipboardData)
            .map((rawFile) => ({ rawFile, uploadFile: normalizeClipboardFile(rawFile) }))
            .filter(({ uploadFile }) => fileMatchesAcceptedType(uploadFile, acceptedTypes));
        if (!clipboardFiles.length) {
            return false;
        }

        const { rawFile, uploadFile } = clipboardFiles[0];
        const uploadFiles = createUploadFileList(uploadFile)
            || (
                rawClipboardFiles?.length === 1
                && rawClipboardFiles[0] === rawFile
                && rawFile === uploadFile
                    ? rawClipboardFiles
                    : null
            );
        return assignClipboardFileToUpload(uploadInput, uploadFiles);
    };

    // 修正 Gradio Dropdown 在 fixed 浮层里按视口定位导致的下拉列表漂移。
    const positionAdvancedDropdowns = () => {
        const popover = findPopover();
        if (!popover || !document.body.classList.contains(POPOVER_OPEN_CLASS)) {
            return;
        }

        popover.querySelectorAll("ul.options").forEach((options) => {
            const wrap = options.closest(".wrap");
            if (!wrap) {
                return;
            }

            popover.querySelectorAll(".wrap").forEach((item) => {
                item.style.removeProperty("z-index");
            });

            const wrapRect = wrap.getBoundingClientRect();
            const popoverRect = popover.getBoundingClientRect();
            const viewportPadding = 12;
            const gap = 6;
            const belowSpace = Math.max(0, popoverRect.bottom - wrapRect.bottom - viewportPadding);
            const aboveSpace = Math.max(0, wrapRect.top - popoverRect.top - viewportPadding);
            const naturalHeight = Math.max(36, Math.min(options.scrollHeight || 220, 240));
            const openBelow = belowSpace >= Math.min(180, naturalHeight) || belowSpace >= aboveSpace;
            const availableHeight = Math.max(84, openBelow ? belowSpace : aboveSpace);
            const height = Math.min(naturalHeight, availableHeight);
            const top = openBelow ? wrap.offsetHeight + gap : -height - gap;

            wrap.style.setProperty("z-index", "1003", "important");
            options.style.setProperty("position", "absolute", "important");
            options.style.setProperty("left", "0", "important");
            options.style.setProperty("top", `${top}px`, "important");
            options.style.setProperty("bottom", "auto", "important");
            options.style.setProperty("width", `${wrapRect.width}px`, "important");
            options.style.setProperty("max-height", `${height}px`, "important");
            options.style.setProperty("z-index", "1004", "important");
        });
    };

    // 只在真正支持鼠标悬浮的桌面环境启用 hover 浮窗，触屏设备继续使用点击兜底。
    const supportsHoverPopover = () => (
        typeof window.matchMedia === "function"
        && window.matchMedia("(hover: hover) and (pointer: fine)").matches
    );

    // 取消尚未执行的打开/关闭计时，避免鼠标在按钮和气泡之间移动时闪烁。
    const cancelPopoverTimers = () => {
        if (openTimer !== null) {
            clearTimeout(openTimer);
            openTimer = null;
        }
        if (closeTimer !== null) {
            clearTimeout(closeTimer);
            closeTimer = null;
        }
        if (visibilityTimer !== null) {
            clearTimeout(visibilityTimer);
            visibilityTimer = null;
        }
    };

    // 清理旧版 display 开关留下的内联样式，后续统一交给 CSS 的可见性和动画状态控制。
    const clearLegacyPopoverDisplay = (popover) => {
        if (popover) {
            popover.style.removeProperty("display");
        }
    };

    // 用内联 important 同步动画属性，避免 Gradio 自动 scoped CSS 抬高隐藏规则优先级。
    const applyOpenPopoverStyle = (popover) => {
        if (!popover) {
            return;
        }
        popover.style.setProperty("visibility", "visible", "important");
        popover.style.setProperty("opacity", "1", "important");
        popover.style.setProperty("pointer-events", "auto", "important");
        popover.style.setProperty("transform", "translateY(0) scale(1)", "important");
    };

    // 关闭时先取消交互并播放淡出，动画结束后再隐藏可见性。
    const applyClosedPopoverStyle = (popover) => {
        if (!popover) {
            return;
        }
        popover.style.setProperty("opacity", "0", "important");
        popover.style.setProperty("pointer-events", "none", "important");
        popover.style.setProperty("transform", "translateY(-4px) scale(0.985)", "important");
        visibilityTimer = window.setTimeout(() => {
            if (!document.body.classList.contains(POPOVER_OPEN_CLASS)) {
                popover.style.setProperty("visibility", "hidden", "important");
            }
            visibilityTimer = null;
        }, ANIMATION_DELAY_MS);
    };

    // 等待 Gradio 完成下拉列表挂载后，再按当前输入框位置校正。
    const queueDropdownPosition = () => {
        requestAnimationFrame(() => {
            requestAnimationFrame(positionAdvancedDropdowns);
        });
    };

    // 根据高级选项按钮的位置，把气泡贴在左侧控制栏右侧并限制在视口内。
    const positionPopover = () => {
        const button = findButton();
        const popover = findPopover();
        if (!button || !popover) {
            return;
        }

        const buttonRect = button.getBoundingClientRect();
        const preferredWidth = Math.min(420, window.innerWidth - 36);
        const left = Math.min(
            Math.max(18, buttonRect.right + 12),
            Math.max(18, window.innerWidth - preferredWidth - 18)
        );
        const availableHeight = Math.max(260, window.innerHeight - 36);
        const measuredHeight = Math.min(
            popover.scrollHeight || 520,
            availableHeight,
            Math.round(window.innerHeight * 0.7)
        );
        const centeredTop = buttonRect.top + buttonRect.height / 2 - measuredHeight / 2;
        const top = Math.min(
            Math.max(18, centeredTop),
            Math.max(18, window.innerHeight - measuredHeight - 18)
        );

        popover.style.setProperty("--mineru-popover-left", `${left}px`);
        popover.style.setProperty("--mineru-popover-top", `${top}px`);
    };

    // 打开气泡时保持组件 DOM 挂载，只切换 body 状态类并重新计算位置。
    const openPopover = () => {
        const popover = findPopover();
        cancelPopoverTimers();
        clearLegacyPopoverDisplay(popover);
        document.body.classList.add(POPOVER_OPEN_CLASS);
        applyOpenPopoverStyle(popover);
        requestAnimationFrame(() => {
            positionPopover();
            queueDropdownPosition();
        });
    };

    // 收起气泡时不卸载 Gradio 控件，用户已经修改的高级配置会保留在原组件上。
    const closePopover = () => {
        const popover = findPopover();
        cancelPopoverTimers();
        clearLegacyPopoverDisplay(popover);
        document.body.classList.remove(POPOVER_OPEN_CLASS);
        applyClosedPopoverStyle(popover);
    };

    // 鼠标进入按钮后延迟打开，防止只是路过按钮时频繁弹出。
    const scheduleHoverOpen = () => {
        if (!supportsHoverPopover()) {
            return;
        }
        cancelPopoverTimers();
        openTimer = window.setTimeout(() => {
            openTimer = null;
            openPopover();
        }, OPEN_DELAY_MS);
    };

    // 鼠标离开按钮或气泡后延迟关闭，给用户从按钮移动到气泡留出缓冲时间。
    const scheduleHoverClose = () => {
        if (!supportsHoverPopover()) {
            return;
        }
        cancelPopoverTimers();
        closeTimer = window.setTimeout(() => {
            closeTimer = null;
            closePopover();
        }, CLOSE_DELAY_MS);
    };

    // 给真实桌面指针安装 hover 事件；如果 Gradio 稍后才挂载 DOM，就通过观察器重试。
    const installHoverPopoverHandlers = () => {
        if (hoverHandlersInstalled || !supportsHoverPopover()) {
            return;
        }
        const button = findButton();
        const popover = findPopover();
        if (!button || !popover) {
            return;
        }
        button.addEventListener("pointerenter", scheduleHoverOpen);
        button.addEventListener("pointerleave", scheduleHoverClose);
        button.addEventListener("mouseenter", scheduleHoverOpen);
        button.addEventListener("mouseleave", scheduleHoverClose);
        popover.addEventListener("pointerenter", cancelPopoverTimers);
        popover.addEventListener("pointerleave", scheduleHoverClose);
        popover.addEventListener("mouseenter", cancelPopoverTimers);
        popover.addEventListener("mouseleave", scheduleHoverClose);
        hoverHandlersInstalled = true;
    };

    refreshMineruCustomHtml();
    installHoverPopoverHandlers();
    requestAnimationFrame(() => {
        refreshMineruCustomHtml();
        installHoverPopoverHandlers();
    });
    if (typeof MutationObserver !== "undefined") {
        const uiObserver = new MutationObserver(() => {
            refreshMineruCustomHtml();
            installHoverPopoverHandlers();
        });
        uiObserver.observe(document.body, { childList: true, subtree: true });
    }

    document.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof Element)) {
            return;
        }
        queueMineruOptionVisibilityRefresh();
        const languageOption = target.closest(".mineru-language-option");
        if (languageOption) {
            selectUiLocale(languageOption.getAttribute("data-mineru-locale"));
            return;
        }
        if (target.closest(".office-preview-ignore-forever")) {
            const notice = target.closest(".office-preview-notice");
            if (setOfficePreviewNoticeIgnored()) {
                applyOfficePreviewNoticePreference();
            } else {
                notice?.classList.add("is-dismissed");
            }
            return;
        }
        if (target.closest(".office-preview-ignore-once")) {
            target.closest(".office-preview-notice")?.classList.add("is-dismissed");
            return;
        }
        if (target.closest(".mineru-advanced-open")) {
            if (document.body.classList.contains(POPOVER_OPEN_CLASS)) {
                closePopover();
            } else {
                openPopover();
            }
            return;
        }
        if (target.closest(".mineru-advanced-popover")) {
            queueDropdownPosition();
        }
        if (!target.closest(".mineru-advanced-popover")) {
            closePopover();
        }
    });

    document.addEventListener("focusin", (event) => {
        const target = event.target;
        if (target instanceof Element && target.closest(".mineru-advanced-popover")) {
            queueDropdownPosition();
        }
    });

    document.addEventListener("input", (event) => {
        const target = event.target;
        queueMineruOptionVisibilityRefresh();
        if (target instanceof Element && target.closest(".mineru-advanced-popover")) {
            queueDropdownPosition();
        }
    });

    document.addEventListener("change", () => {
        queueMineruOptionVisibilityRefresh();
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closePopover();
            return;
        }
        const target = event.target;
        if (target instanceof Element && target.closest(".mineru-advanced-popover")) {
            queueDropdownPosition();
        }
    });

    document.addEventListener("paste", (event) => {
        if (uploadClipboardFile(event)) {
            event.preventDefault();
        }
    });

    window.addEventListener("resize", () => {
        if (document.body.classList.contains(POPOVER_OPEN_CLASS)) {
            positionPopover();
            positionAdvancedDropdowns();
        }
    });

    const EDITOR_ZOOM_SCRIPT_VERSION = "editor-zoom-v2";
    if (window.__mineruEditorZoomInstalled !== EDITOR_ZOOM_SCRIPT_VERSION) {
        window.__mineruEditorZoomInstalled = EDITOR_ZOOM_SCRIPT_VERSION;

        const EDITOR_ZOOM_MIN = 1;
        const EDITOR_ZOOM_MAX = 4;
        const EDITOR_PAN_CLICK_THRESHOLD = 5; // px — ниже этого считаем кликом, не паном
        let editorZoomState = { scale: 1, tx: 0, ty: 0 };
        let editorPanState = null;

        const findEditorPageEl = (target) => (target && target.closest ? target.closest(".mineru-editor-page") : null);

        const applyEditorZoomVars = (container) => {
            container.style.setProperty("--mineru-ez-scale", String(editorZoomState.scale));
            container.style.setProperty("--mineru-ez-tx", `${editorZoomState.tx}px`);
            container.style.setProperty("--mineru-ez-ty", `${editorZoomState.ty}px`);
        };

        const resetEditorZoom = () => {
            editorZoomState = { scale: 1, tx: 0, ty: 0 };
            document.querySelectorAll(".mineru-editor-page").forEach(applyEditorZoomVars);
        };

        const zoomEditorAt = (container, clientX, clientY, nextScaleRaw) => {
            const nextScale = Math.min(EDITOR_ZOOM_MAX, Math.max(EDITOR_ZOOM_MIN, nextScaleRaw));
            const rect = container.getBoundingClientRect();
            const originX = clientX - rect.left;
            const originY = clientY - rect.top;
            const prevScale = editorZoomState.scale;
            const localX = (originX - editorZoomState.tx) / prevScale;
            const localY = (originY - editorZoomState.ty) / prevScale;
            editorZoomState = {
                scale: nextScale,
                tx: originX - localX * nextScale,
                ty: originY - localY * nextScale,
            };
            if (nextScale <= EDITOR_ZOOM_MIN) {
                editorZoomState.tx = 0;
                editorZoomState.ty = 0;
            }
            applyEditorZoomVars(container);
        };

        window.mineruEditorZoomIn = () => {
            const container = document.querySelector(".mineru-editor-page");
            if (!container) return;
            const rect = container.getBoundingClientRect();
            zoomEditorAt(container, rect.left + rect.width / 2, rect.top + rect.height / 2, editorZoomState.scale * 1.3);
        };
        window.mineruEditorZoomOut = () => {
            const container = document.querySelector(".mineru-editor-page");
            if (!container) return;
            const rect = container.getBoundingClientRect();
            zoomEditorAt(container, rect.left + rect.width / 2, rect.top + rect.height / 2, editorZoomState.scale / 1.3);
        };
        window.mineruEditorZoomFit = () => resetEditorZoom();
        window.mineruEditorZoomActual = () => {
            const container = document.querySelector(".mineru-editor-page");
            if (!container) return;
            const rect = container.getBoundingClientRect();
            zoomEditorAt(container, rect.left + rect.width / 2, rect.top + rect.height / 2, 2);
        };

        document.addEventListener("wheel", (event) => {
            const container = findEditorPageEl(event.target);
            if (!container) return;
            event.preventDefault();
            const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
            zoomEditorAt(container, event.clientX, event.clientY, editorZoomState.scale * factor);
        }, { passive: false });

        document.addEventListener("pointerdown", (event) => {
            const container = findEditorPageEl(event.target);
            if (!container || event.button !== 0) return;
            editorPanState = {
                startX: event.clientX,
                startY: event.clientY,
                startTx: editorZoomState.tx,
                startTy: editorZoomState.ty,
                moved: false,
                pointerId: event.pointerId,
                container,
            };
            if (container.setPointerCapture) {
                try { container.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
            }
            container.classList.add("mineru-ez-panning");
        });

        document.addEventListener("pointermove", (event) => {
            if (!editorPanState || event.pointerId !== editorPanState.pointerId) return;
            const dx = event.clientX - editorPanState.startX;
            const dy = event.clientY - editorPanState.startY;
            if (!editorPanState.moved && Math.hypot(dx, dy) > EDITOR_PAN_CLICK_THRESHOLD) {
                editorPanState.moved = true;
            }
            if (editorPanState.moved) {
                editorZoomState.tx = editorPanState.startTx + dx;
                editorZoomState.ty = editorPanState.startTy + dy;
                applyEditorZoomVars(editorPanState.container);
            }
        });

        const endEditorPan = (event) => {
            if (!editorPanState || (event.pointerId !== undefined && event.pointerId !== editorPanState.pointerId)) return;
            editorPanState.container.classList.remove("mineru-ez-panning");
            if (editorPanState.moved) {
                editorPanState.container.__mineruSuppressNextClick = true;
            }
            editorPanState = null;
        };
        document.addEventListener("pointerup", endEditorPan);
        document.addEventListener("pointercancel", endEditorPan);

        document.addEventListener("click", (event) => {
            if (event.defaultPrevented) return;
            const container = findEditorPageEl(event.target);
            if (container && container.__mineruSuppressNextClick) {
                container.__mineruSuppressNextClick = false;
                event.stopPropagation();
                event.preventDefault();
            }
        }, true); // capture — раньше внутреннего click-обработчика Gradio Image .select()

        // Gradio 6 периодически не испускает Image.select() для уже
        // отрендеренного сервером изображения. Сохраняем координаты локально,
        // а обычная Button.click() передаёт их серверному обработчику через js=.
        const showEditorBlockPending = (container, image, clientX, clientY) => {
            document.querySelectorAll(".mineru-editor-pending-marker").forEach((marker) => marker.remove());
            if (window.__mineruEditorPendingMarkerTimer) {
                window.clearTimeout(window.__mineruEditorPendingMarkerTimer);
            }
            const containerRect = container.getBoundingClientRect();
            const marker = document.createElement("div");
            marker.className = "mineru-editor-pending-marker";
            marker.style.left = `${clientX - containerRect.left}px`;
            marker.style.top = `${clientY - containerRect.top}px`;
            container.append(marker);
            window.__mineruEditorPendingMarkerTimer = window.setTimeout(() => marker.remove(), 15000);
            image.addEventListener("load", () => marker.remove(), { once: true });

            const statusRoot = document.querySelector("#mineru-editor-status");
            const statusTarget = statusRoot?.querySelector(".html-container, .prose") || statusRoot;
            if (statusTarget) {
                statusTarget.innerHTML = (
                    '<div class="mineru-editor-pending-status">'
                    + '<span class="mineru-editor-pending-spinner" aria-hidden="true"></span>'
                    + '<span>Загрузка выбранного блока…</span>'
                    + '</div>'
                );
            }
        };
        document.addEventListener("click", (event) => {
            if (event.defaultPrevented) return;
            const container = findEditorPageEl(event.target);
            const image = container && (
                (event.target instanceof Element ? event.target.closest("img") : null)
                || container.querySelector("img")
            );
            if (!container || !image || !container.contains(image)) return;
            const rect = image.getBoundingClientRect();
            if (!rect.width || !rect.height || !image.naturalWidth || !image.naturalHeight) return;
            // <img> занимает всю высоту редактора, а сама страница внутри него
            // рисуется через object-fit: contain. Считаем не от внешнего rect,
            // а от фактической области пикселей документа; иначе в верхнем и
            // нижнем пустом поле выбирается блок с неверной координатой Y.
            const displayScale = Math.min(
                rect.width / image.naturalWidth,
                rect.height / image.naturalHeight,
            );
            const renderedWidth = image.naturalWidth * displayScale;
            const renderedHeight = image.naturalHeight * displayScale;
            const renderedLeft = rect.left + (rect.width - renderedWidth) / 2;
            const renderedTop = rect.top + (rect.height - renderedHeight) / 2;
            const x = (event.clientX - renderedLeft) / displayScale;
            const y = (event.clientY - renderedTop) / displayScale;
            if (x < 0 || y < 0 || x > image.naturalWidth || y > image.naturalHeight) return;
            const trigger = document.querySelector("#mineru-editor-select-trigger button, #mineru-editor-select-trigger");
            if (!trigger) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            showEditorBlockPending(container, image, event.clientX, event.clientY);
            window.__mineruEditorClickXY = JSON.stringify({ x, y });
            window.setTimeout(() => trigger.click(), 0);
        }, true);

        document.addEventListener("keydown", (event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            const active = document.activeElement;
            const activeTag = active && active.tagName;
            if (activeTag === "INPUT" || activeTag === "TEXTAREA" || (active && active.isContentEditable)) return;
            const editorPage = document.querySelector(".mineru-editor-page");
            if (!editorPage || editorPage.offsetParent === null) return; // редактор скрыт
            const targetSelector = event.key === "ArrowLeft" ? ".mineru-editor-prev" : ".mineru-editor-next";
            const targetButton = document.querySelector(targetSelector);
            if (targetButton) {
                event.preventDefault();
                targetButton.click();
            }
        });
    }
}
