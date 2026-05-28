# Reassign CRM owner

Разовая операция для переноса сущностей Bitrix CRM с одного ответственного на другого.

## Workflow

`Actions → Reassign CRM owner`

## Рекомендуемый первый запуск

```text
source_user_id = ID старого менеджера
target_user_id = ID нового менеджера
dry_run = true
include_companies = true
include_contacts = true
include_leads = true
include_deals = false
include_closed_deals = false
deal_category_id = all
limit = 0
```

После проверки artifact `reassign-crm-owner-log` можно запускать:

```text
dry_run = false
```

## Важно

По умолчанию сделки не переносит. Для полного переезда менеджера включить:

```text
include_deals = true
include_closed_deals = true/false по ситуации
```
