# Конфигурация распределения e-Qazyna / Bitrix

Правила распределения вынесены из Python-кода в YAML-файлы. Теперь основные ручные изменения делаются в папке:

```text
eqazyna_bitrix/config/
```

## 1. Менеджеры

Файл:

```text
eqazyna_bitrix/config/managers.yml
```

Что хранит:

- технических пользователей, с которых списываем заявки (`36`, `44`);
- активных менеджеров распределения;
- ФИО менеджера;
- ID филиала;
- название филиала;
- флаг `active`.

Добавление нового менеджера:

```yaml
  - id: 106
    name: Алина Курбанова
    branch_id: 18
    branch: Талдыкорган
    active: true
```

Чтобы временно убрать менеджера из автоматического распределения, не удаляй строку. Поставь:

```yaml
active: false
```

## 2. Жёсткие БИНы

Файл:

```text
eqazyna_bitrix/config/hard_bins.yml
```

Что хранит:

```yaml
hard_bin_owners:
  - user_id: 78
    bins:
      - "240640023673"
      - "250840017668"
      - "260240027813"
```

Если один БИН указан у нескольких менеджеров, аудит помечает его как конфликтный hard-БИН. При `duplicate_hard_bin_policy = skip` такие пакеты не трогаются.

## 3. Ручные фиксации руководителей

Файл:

```text
eqazyna_bitrix/config/manual_directors.yml
```

Что хранит:

```yaml
manual_director_owners:
  - user_id: 70
    directors:
      - ЖИЛКАШИНОВА АСЕЛЬ МИХАЙЛОВНА
      - ЖЫЛКАШИНОВА АСЕЛЬ МИХАЙЛОВНА
```

Эти правила сильнее массового распределения. Если руководитель найден, весь пакет компаний/сделок приводится к указанному менеджеру.

## 4. Что больше не надо править руками

В обычной работе не нужно редактировать:

```text
eqazyna_bitrix/distribute_companies.py
eqazyna_bitrix/audit_repair_deal_packages.py
eqazyna_bitrix/manual_director_fix_packages.py
eqazyna_bitrix/pipeline.py
```

Эти файлы должны выполнять логику, а не хранить бизнес-справочники.

## 5. Контроль после правок

После изменения YAML-конфига сначала запускай dry-run:

```text
dry_run = true
```

Проверяй в JSON-логе:

```text
allowed_user_ids
allowed_users
hard_bin_count
manual_director_owners
```

После проверки можно запускать запись.
