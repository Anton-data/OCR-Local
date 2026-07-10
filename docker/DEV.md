# MinerU: локальная разработка в Docker

Конфигурация рассчитана на Windows 10 + Docker Desktop/WSL2 и NVIDIA GPU.
Исходный код монтируется в контейнер, а пакет MinerU установлен в editable-режиме.

## Сборка и запуск

```powershell
docker compose -f docker/compose.dev.yaml build mineru-api
docker compose -f docker/compose.dev.yaml --profile models run --rm mineru-models
docker compose -f docker/compose.dev.yaml up -d mineru-api
```

REST API и Swagger UI:

- API: `http://127.0.0.1:8001`
- Swagger: `http://127.0.0.1:8001/docs`
- Проверка состояния: `http://127.0.0.1:8001/health`

API запущен с `--reload`: изменения Python-файлов в рабочей копии применяются
автоматически. После изменения зависимостей или Dockerfile образ нужно пересобрать.

## Модели

По умолчанию загружаются все локальные модели. Для экономии места можно загрузить
только один набор:

```powershell
$env:MINERU_MODEL_TYPE = "vlm"
docker compose -f docker/compose.dev.yaml --profile models run --rm mineru-models
Remove-Item Env:MINERU_MODEL_TYPE
```

Допустимые значения: `pipeline`, `vlm`, `all`. Модели и конфигурация хранятся в
именованных Docker-томах и не удаляются при пересборке контейнера.

## Управление

```powershell
docker compose -f docker/compose.dev.yaml logs -f mineru-api
docker compose -f docker/compose.dev.yaml restart mineru-api
docker compose -f docker/compose.dev.yaml down
```

Gradio запускается отдельно:

```powershell
docker compose -f docker/compose.dev.yaml --profile gradio up -d mineru-gradio
```

Интерфейс будет доступен на `http://127.0.0.1:7860`.
