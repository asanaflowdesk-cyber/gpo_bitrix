# RUNBOOK

## Проверить Bitrix

Actions → Bitrix diagnostics → Run workflow.

Ожидаемый результат: artifact `bitrix-diagnostics` с JSON-файлами.

## Проверить eGov по одному БИН

Actions → eGov BIN diagnostics → Run workflow.

Вставить БИН, например:

```text
260340002871
```

Если результат `egov_forbidden_403`, проблема в доступе к `data.egov.kz`, а не в e-Qazyna и не в Bitrix.

## Запустить основной поток вручную

Actions → e-Qazyna to Bitrix CRM AUTO → Run workflow.

Для безопасной проверки:

```text
push_bitrix = true
dry_run = true
```

Для записи в CRM:

```text
push_bitrix = true
dry_run = false
```

## Проверить расписание

Workflow запускается по cron:

```text
3/15 * * * *
```

Это 03, 18, 33, 48 минуты каждого часа по UTC. GitHub может задерживать scheduled runs; это нормальное поведение платформы.

Scheduled workflow работает только из default branch. Проверь:

```text
Settings → Branches → Default branch = main
```

и что файл лежит здесь:

```text
.github/workflows/main.yml
```

## Если сделки не появились

Проверить последний run:

```text
Found applications after filter
created_company_created_deal
existing_company_created_deal
existing_company_existing_deal
error
```

`existing_company_existing_deal` — не ошибка. Это защита от дублей.
