# Настройка Bitrix24

## 1. Создать входящий webhook

Путь в Bitrix24 обычно такой:

```text
Приложения → Разработчикам → Другое → Входящий вебхук
```

Права вебхука:

```text
CRM
```

Скопировать URL вида:

```text
https://your-portal.bitrix24.kz/rest/USER_ID/WEBHOOK_CODE/
```

Этот URL добавить в GitHub Secret `BITRIX_WEBHOOK_URL`.

## 2. Компания

Интеграция создаёт компании в:

```text
CRM → Клиенты → Компании
```

Компания ищется по БИН через технические поля:

```text
ORIGINATOR_ID = EQAZYNA
ORIGIN_ID = БИН
```

## 3. Сделка

Каждая заявка e-Qazyna создаётся как сделка и привязывается к компании.

Название сделки:

```text
e-Qazyna № номер заявки — название компании
```

## 4. Реквизиты и БИН

Чтобы БИН попадал в блок «Реквизиты», нужен ID шаблона реквизитов.

Проще получить его через workflow:

```text
Actions → Bitrix diagnostics → Run workflow
```

Скачать artifact `bitrix-diagnostics`, открыть:

```text
requisite_presets.json
```

Найти нужный шаблон организации и взять его `ID`.

Потом добавить в GitHub Variables:

```text
BITRIX_REQUISITE_PRESET_ID = найденный ID
BITRIX_REQUISITE_BIN_FIELD = RQ_BIN
```

Если `RQ_BIN` не сработает на вашем портале, открыть `requisite_fields.json` из diagnostics и посмотреть точное имя поля для БИН.

## 5. Воронка и стадия сделки

По умолчанию:

```text
CATEGORY_ID = 0
STAGE_ID = NEW
```

Если нужна воронка с `CATEGORY_ID = 2`, использовать стадию вида:

```text
C2:NEW
```

В твоём списке статусов уже видна стадия `C2:NEW` для `DEAL_STAGE_2`, поэтому для второй воронки это вероятный стартовый вариант.
