# Настройка GitHub

## 1. Загрузить проект

В корне репозитория должны лежать:

```text
.github/
eqazyna_bitrix/
requirements.txt
README.md
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
BITRIX_DEAL_CATEGORY_ID        # например 0 или 2
BITRIX_DEAL_STAGE_ID           # например NEW или C2:NEW
BITRIX_ASSIGNED_BY_ID          # ID ответственного, опционально
BITRIX_REQUISITE_PRESET_ID     # ID шаблона реквизитов, нужен для записи БИН в реквизиты
BITRIX_REQUISITE_BIN_FIELD     # обычно RQ_BIN
```

## 4. Ручной запуск

```text
Actions → e-Qazyna to Bitrix CRM → Run workflow
```

Для первого теста:

```text
pages = 2
push_bitrix = true
dry_run = true
no_egov = false
```

Если Excel-лог выглядит нормально, следующий запуск:

```text
dry_run = false
```

## 5. Автозапуск

Workflow уже настроен на запуск каждые 15 минут:

```yaml
schedule:
  - cron: "*/15 * * * *"
```

Если пока не нужен автозапуск, закомментировать блок `schedule` в `.github/workflows/eqazyna-to-bitrix.yml`.

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
