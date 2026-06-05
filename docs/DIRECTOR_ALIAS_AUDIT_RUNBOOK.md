# Audit director alias duplicates

Read-only проверка случаев, где один руководитель может быть заведен разными вариантами имени:

- `ЛЯБАХ Г.Г.`
- `Геннадий Лябах`
- `Лябах Геннадий`

Скрипт строит alias-ключ вида `ФАМИЛИЯ|ПЕРВАЯ_БУКВА_ИМЕНИ` и выводит группы, где несколько карточек/сделок/компаний похожи на одного человека.

## Workflow

`Actions → Audit director alias duplicates`

Рекомендуемый тест:

```text
only_eqazyna = true
include_closed_deals = true
include_contacts = true
include_companies = true
include_deals = true
max_contacts = 0
max_companies = 0
max_deals = 0
min_records_per_group = 2
```

## Artifacts

- `director_alias_groups.csv` — сводка по alias-группам.
- `director_alias_suspects.csv` — только подозрительные записи для ручной проверки.
- `director_alias_records.csv` — все найденные записи.

Главный файл для проверки: `director_alias_suspects.csv`.

Скрипт ничего не меняет в Bitrix.
