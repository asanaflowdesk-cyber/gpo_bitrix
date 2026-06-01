# Reassign CRM owner

Разовая ручная передача сущностей Bitrix от одного ответственного к другому.

## Основной сценарий

1. Сначала запускать `dry_run=true`.
2. Проверить artifact `reassign_crm_owner_log.csv`.
3. Только потом запускать `dry_run=false`.

## Полезные параметры

- `source_user_id` — старый менеджер.
- `target_user_id` — новый менеджер.
- `limit` — лимит на каждый тип сущности.
- `max_total` — общий лимит по всем выбранным сущностям.
- `deal_stage_ids` — стадии сделок через запятую, например `NEW,EXECUTING,UC_HRSCUK`.
- `lead_status_ids` — статусы лидов через запятую.
- `filter_company_contact_by_deal_stage=true` — компании и контакты переносить только если у них есть связанная сделка старого менеджера в выбранной стадии.

## Пример

Передать 10 компаний/контактов/сделок со стадии `NEW`:

- `include_companies=true`
- `include_contacts=true`
- `include_leads=false`
- `include_deals=true`
- `deal_stage_ids=NEW`
- `filter_company_contact_by_deal_stage=true`
- `max_total=10`

