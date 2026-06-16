# Update company phones by BIN

Назначение: разово загрузить телефоны в существующие карточки компаний Bitrix по БИН.

Файл импорта должен быть `.xlsx` или `.csv` с колонками:

- `BIN`
- `MOBILE`

Один БИН обрабатывается один раз. После записи в `COMMENTS` компании добавляется маркер:

```text
AUTO_PHONE_IMPORT_BIN:<BIN>
```

Если этот же БИН попадёт в следующий файл, workflow пропустит его, если `force=false`.

## Запуск

1. Положить файл в репозиторий, например:

```text
imports/company_phones.xlsx
```

2. Запустить:

```text
Actions → Update company phones by BIN
file_path = imports/company_phones.xlsx
dry_run = true
force = false
```

3. Проверить artifact `update-company-phones-log`.

4. Если всё корректно:

```text
dry_run = false
```

## Важно

Скрипт не создаёт новые компании. Если компания по БИН не найдена, строка попадёт в лог как `company_not_found`.
