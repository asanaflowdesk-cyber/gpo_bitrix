# Карта полей Bitrix24

## CRM-модель

- Компания = клиент.
- Сделка = конкретная заявка e-Qazyna.
- Один БИН = одна компания.
- У одной компании может быть несколько сделок.

## Компания: стандартные поля

| Bitrix field | Что пишем |
|---|---|
| TITLE | Название компании из eGov или e-Qazyna |
| COMPANY_TYPE | CUSTOMER |
| ORIGINATOR_ID | EQAZYNA |
| ORIGIN_ID | БИН |
| REG_ADDRESS | Юридический адрес из eGov |
| REG_ADDRESS_CITY | Город, если распознан |
| REG_ADDRESS_PROVINCE | Область/регион, если распознан |
| REG_ADDRESS_COUNTRY | Казахстан |
| COMMENTS | Сводка по компании, eGov и последней заявке |
| OPENED | Y |
| ASSIGNED_BY_ID | Опционально, если задана переменная BITRIX_ASSIGNED_BY_ID |

## Компания: реквизиты

В реквизиты пишется БИН.

Для создания реквизитов нужен ID шаблона реквизитов Bitrix24:

- переменная GitHub: BITRIX_REQUISITE_PRESET_ID
- поле БИН: BITRIX_REQUISITE_BIN_FIELD, по умолчанию RQ_BIN

Если BITRIX_REQUISITE_PRESET_ID не задан, интеграция не падает: компания и сделка будут созданы, но реквизит будет пропущен.

## Сделка: стандартные поля

| Bitrix field | Что пишем |
|---|---|
| TITLE | e-Qazyna № номер заявки — название компании |
| COMPANY_ID | ID компании в Bitrix |
| CATEGORY_ID | ID воронки, по умолчанию 0 |
| STAGE_ID | стадия, по умолчанию NEW |
| COMMENTS | Полная карточка заявки + eGov-обогащение + ссылки на поиск контактов |
| ORIGINATOR_ID | EQAZYNA |
| ORIGIN_ID | eQazyna\|номер заявки\|БИН |
| SOURCE_ID | OTHER |
| SOURCE_DESCRIPTION | e-Qazyna minerals registry |

## Дедупликация

Компания ищется по:

```text
ORIGINATOR_ID = EQAZYNA
ORIGIN_ID = БИН
```

Сделка ищется по:

```text
ORIGINATOR_ID = EQAZYNA
ORIGIN_ID = eQazyna|номер заявки|БИН
```

Так одна компания не размножается, а каждая новая заявка становится отдельной сделкой.
