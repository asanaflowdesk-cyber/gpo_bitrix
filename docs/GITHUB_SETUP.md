# Настройка GitHub

## 1. Загрузить проект

В корне репозитория должны лежать:

```text
.github/
eqazyna_bitrix/
requirements.txt
docs/
```

Нельзя загружать папку проекта внутрь ещё одной папки, иначе GitHub Actions не увидит `.github/workflows`.

## 2. Secrets

Открыть:

```text
Repository → Settings → Secrets and variables → Actions → Secrets → New repository secret
```

Добавить:

```text
EGOV_API_KEY
BITRIX_WEBHOOK_URL
```

## 3. Variables

Открыть:

```text
Repository → Settings → Secrets and variables → Actions → Variables → New repository variable
```

Добавить при необходимости:

```text
BITRIX_DEAL_CATEGORY_ID
BITRIX_DEAL_STAGE_ID
BITRIX_REQUISITE_PRESET_ID
BITRIX_REQUISITE_BIN_FIELD
BITRIX_ASSIGNMENT_LIMIT_PER_MANAGER    # по умолчанию 30 сделок
BITRIX_ASSIGNMENT_LOAD_STAGE_IDS       # стадии, которые входят в лимит: Новая + В работе
```

`BITRIX_ASSIGNED_BY_ID` больше не должен использоваться как способ массово назначать новые заявки одному человеку. Новые руководители распределяются по минимальной активной нагрузке, а исторические руководители идут своему историческому менеджеру.

## 4. Ручной запуск

```text
Actions → e-Qazyna to Bitrix CRM AUTO → Run workflow
```

Для первого теста:

```text
pages = 3
push_bitrix = true
dry_run = true
no_egov = false
```

Если Excel/JSON-лог выглядит нормально, следующий запуск:

```text
dry_run = false
```

## 5. Автозапуск

Внутренний GitHub `schedule` из workflow убран, чтобы не было двойного запуска. Регулярный запуск должен идти через внешний cron schedule site.

Если cron site вызывает GitHub workflow dispatch, он должен запускать `.github/workflows/main.yml` с нужными input-параметрами или использовать значения по умолчанию. В `main.yml` значение по умолчанию для `dry_run` — `false`, потому что регулярный cron рассчитан на боевую запись. Для проверки руками всегда явно ставить `dry_run=true`.

## 6. Логи

После каждого запуска GitHub создаёт artifact:

```text
eqazyna-bitrix-run-log
```

Внутри:

```text
eqazyna_bitrix_log.xlsx
eqazyna_bitrix_log.json
```
