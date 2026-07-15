# CLAUDE.md — служебные заметки по репозиторию MinerU

Ориентировка для будущих сессий Claude Code в этом репозитории: что тут
нестандартного, что было сделано и на что смотреть при следующих правках.
Не туториал — конкретика этого чекаута.

## Dev-инфраструктура (Docker)

Запуск/остановка/команды — см. `docker/DEV.md`, не дублируется здесь.

Специфика, которой в DEV.md нет:

- `docker/compose.dev.yaml` (проект `mineru-dev`) монтирует весь репозиторий
  живьём: `../:/workspace/MinerU` в каждом сервисе (`x-mineru-common`).
  Значит **правки Python-кода применяются рестартом контейнера**
  (`docker compose -f docker/compose.dev.yaml restart mineru-api mineru-gradio`),
  без пересборки образа. `docker compose ... build` нужен только при
  изменении зависимостей или `docker/dev/Dockerfile`.
- Образ (`docker/dev/Dockerfile`) собирается от `vllm/vllm-openai:v0.21.0`,
  MinerU ставится `pip install -e ".[core]"`.
- GPU пробрасывается через `deploy.resources.reservations.devices`
  (`driver: nvidia`, `device_ids: ["0"]`) в `x-mineru-common`, общий для всех
  сервисов. Проверено рабочим на RTX 5070 Ti (Blackwell, sm_120/12.0) —
  PyTorch 2.11.0+cu130 в образе её поддерживает.
- Зафиксированный фикс совместимости: `pdftext==0.7.1` ломает `pipeline`/
  `hybrid-engine` backend'ы (`TypeError: 'PageChars' object is not iterable`,
  класс `PageChars` без `__iter__`); `vlm-engine` не задет, т.к. не парсит
  текстовый слой PDF. В Dockerfile после установки MinerU принудительно
  ставится `pdftext==0.6.3`. Если ошибка вернётся после апдейта
  зависимостей — смотреть сюда в первую очередь.
- Сервисы: `mineru-api` (контейнер `mineru-api-dev`, порт `8001→8000`,
  стартует по умолчанию, `--reload`), `mineru-gradio` (контейнер
  `mineru-gradio-dev`, порт `7860`, профиль `gradio` —
  `--profile gradio up -d mineru-gradio`), `mineru-models` (профиль
  `models`, разовый загрузчик моделей, не постоянный сервис).

## Визуальный редактор в Gradio-UI

Новая вкладка **"Визуальный редактор"** (`i18n("visual_editor_tab")`) в
`mineru/cli/gradio_app.py`, рядом со вкладками markdown/content-list-json
(добавлена как `gr.Tab` внутри той же группы вкладок результата, `~L2547`).
FineReader-style редактирование: страница рендерится картинкой с цветным
оверлеем зон по типу блока, клик по зоне открывает панель правки текста или
таблицы, правки пишутся сразу в `middle.json` на диске.

UI-элементы вкладки (`gradio_app.py`, `~L2547-2599`):
- `editor_prev_bu` / `editor_next_bu` + `editor_page_label_html` —
  постраничная навигация.
- `editor_image` (`gr.Image`, non-interactive) — рендер страницы с
  оверлеем; клик читается через `.select()` → `editor_image_select`
  (координаты клика в пикселях картинки, `SelectData.index`).
- `editor_block_panel` (скрытая группа) — раскрывается после выбора блока:
  `editor_block_textbox` для текстовых блоков, либо `editor_table_html` +
  скрытый `editor_table_harvest_tb` для таблиц.
- Правка таблицы идёт через `contenteditable` прямо в браузере
  (`visual_editor.make_editable_table_html`); при клике "Применить правку"
  itog HTML вытаскивается из DOM через `js=` callback у
  `editor_apply_table_bu.click(...)` (`innerHTML` элемента
  `#mineru-table-editor`, см. `gradio_app.py` `~L3312-3315`) и кладётся в
  скрытый textbox перед вызовом Python-обработчика — обычный Gradio-input
  не подходит, т.к. contenteditable не синхронизирует значение в
  Python-состояние сам по себе.
- `editor_refresh_exports_bu` — кнопка "Обновить DOCX и PDF", пересобирает
  DOCX/searchable-PDF из текущих файлов на диске и переархивирует
  результат (`editor_refresh_exports`, `~L3206`); НЕ вызывает
  `regenerate_derived_outputs` сама — та уже отработала при сохранении
  правки блока, а кнопка полезна и после ручной правки `content_list.json`
  во вкладке "Список содержимого JSON".
- `editor_dirty_html` / `exports_dirty_state` — индикатор "есть правки, не
  попавшие в экспорт"; выставляется в `True` в
  `editor_apply_text_edit`/`editor_apply_table_edit`, сбрасывается в
  `False` после успешного `editor_refresh_exports`.
- Состояния: `selected_block_state`, `editor_page_state`,
  `exports_dirty_state` (объявлены `~L2410-2412`, сбрасываются в
  `clear_bu.add([...])`).

Новый модуль `mineru/cli/visual_editor.py` — вся логика без Gradio-импортов
(только `middle.json`-словари, PIL-картинки, HTML-строки; юнит-тестируем
отдельно от UI):
- `load_middle_json` / `save_middle_json` — чтение/запись
  `{doc_stem}_middle.json`; при первом сохранении создаёт разовый
  `.bak`-бэкап (без undo/history).
- `render_page_base` — рендерит страницу из `{doc_stem}_origin.pdf` через
  PyMuPDF, адаптивный DPI (до 2000px по длинной стороне), кэширует PNG в
  `_editor_cache/page_{idx}.png`.
- `page_count`, `has_origin_pdf` — навигация/доступность редактора (только
  для PDF/сканов, где рядом лежит origin PDF).
- `list_page_blocks` — список блоков страницы из `para_blocks` (не
  `preproc_blocks`); `discarded_blocks` намеренно не включаются.
- `get_page_size` — `page_size` страницы для пересчёта bbox в пиксели.
- `draw_overlay` — полупрозрачные зоны по типу блока + красная рамка на
  выбранном.
- `hit_test` — клик → индекс блока: сначала наименьший по площади
  содержащий bbox, иначе ближайший центр в радиусе 15px.
- `get_block_text` / `apply_text_edit` — чтение/запись текста блока;
  если число строк правки совпало с исходным — построчная геометрия
  сохраняется, иначе весь текст схлопывается в один `line` с bbox блока
  целиком (осознанное упрощение — searchable-PDF всё равно раскладывает
  такой текст по ink-guided placement).
- `find_table_span` — рекурсивный поиск span'а `type == "table"` с полем
  `html` в поддереве блока (MinerU может вкладывать таблицу глубже одного
  уровня).
- `make_editable_table_html` / `apply_table_edit` — превращение
  `table_html` в contenteditable-разметку и обратное наложение
  отредактированных текстов ячеек на оригинальную HTML-структуру
  (по порядку обхода `td`/`th`); при несовпадении числа ячеек — `ValueError`
  (через contenteditable легально меняется только текст, не структура).

Новая функция `regenerate_derived_outputs()` в
`mineru/cli/client_side_output.py` (`~L100`) — пересборка `.md` /
`content_list.json` / `content_list_v2.json` из **уже финализированного**
`middle.json` после ручной правки. Отдельно от существующей
`regenerate_client_side_outputs()`, потому что та вызывает
`finalize_client_side_middle_json` — это одноразовая финализация staged
middle.json, не идемпотентная; повторный вызов на уже финализированном
`middle.json` (что как раз и есть ситуация после правки в визуальном
редакторе) может испортить данные. `regenerate_derived_outputs` читает
`middle.json` как есть и детерминированно прогоняет `union_make`.
Вызывается из `editor_apply_text_edit`/`editor_apply_table_edit`
(`gradio_app.py`, `~L3101`, `~L3161`) сразу после `save_middle_json`.

## Ключевой архитектурный факт: middle.json — мастер данных

- `{file}_middle.json` — мастер-файл для программных правок распознанного
  текста. `{file}_content_list.json`, `.md`, `_content_list_v2.json` —
  производные, пересобираются из него через `union_make`
  (`_select_union_make` в `client_side_output.py`) /
  `regenerate_derived_outputs`.
- `export_docx_from_result_dir()` (`mineru/utils/docx_export.py`) читает
  текст **только** из `content_list.json`.
- `export_searchable_pdf_from_result_dir()`
  (`mineru/utils/searchable_pdf.py`) читает текст **только** из
  `middle.json` (`para_blocks` → `lines` → `spans`).
- Следствие: правка в одном из двух файлов без пересборки производных
  долетает только до одного из двух экспортов (DOCX либо searchable-PDF,
  не до обоих). Отсюда необходимость синхронизации через `middle.json` как
  единственный мастер и обязательный `regenerate_derived_outputs` после
  каждой правки.
- У таблиц нет пер-ячеечного bbox ни в одном из JSON — только bbox всей
  таблицы целиком; HTML хранится одной строкой (`table_body`/`span.html`).
- Бэкенды дают разную гранулярность `middle.json`: у **hybrid**
  практически всегда 1 line = 1 span = весь блок (детализация не выше, чем
  в `content_list.json`); у **pipeline** — реально многострочные блоки с
  индивидуальным bbox на строку.

## Где смотреть подробности

Развёрнутый дизайн этой фичи (почему `gr.Image`+PIL-оверлей вместо
`gr.AnnotatedImage`, алгоритм схлопывания строк при правке текста,
алгоритм попарного сопоставления ячеек таблицы и т.п.) отдельным
документом не сохранён. За деталями — в сам код
(`mineru/cli/visual_editor.py`, компактный и читаемый) и в git-историю
правок `mineru/cli/gradio_app.py` / `mineru/cli/client_side_output.py`.
