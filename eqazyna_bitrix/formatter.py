from __future__ import annotations

from .models import Application, CompanyEnrichment, utc_now_iso
from .search_links import build_search_links


def build_company_summary(app: Application, enr: CompanyEnrichment, existing_deal_count_note: str | None = None) -> str:
    name = enr.name or app.applicant_name
    links = build_search_links(app.bin, name, enr.city or enr.region)
    parts = [
        "Источник: e-Qazyna / eGov",
        "",
        f"БИН: {app.bin}",
        f"Компания: {name}",
        f"Юридический адрес: {enr.legal_address or 'не найден'}",
        f"Регион: {enr.region or 'не определён'}",
        f"Город: {enr.city or 'не определён'}",
        f"Руководитель: {enr.director or 'не найден'}",
        f"Телефон из eGov: {enr.phone or 'не найден'}",
        f"ОКЭД / деятельность: {_activity_line(enr)}",
        f"Дата регистрации: {enr.registration_date or 'не найдена'}",
        "",
        f"Последняя найденная заявка: {app.doc_number}",
        f"Статус последней заявки: {app.status}",
        f"Дата заявки: {app.created_at_raw}",
    ]
    if existing_deal_count_note:
        parts += [existing_deal_count_note]
    parts += [
        "",
        "Ручной поиск контактов:",
        f"2GIS: {links['2gis']}",
        f"Google: {links['google']}",
        f"Yandex: {links['yandex']}",
        "",
        f"Обновлено интеграцией: {utc_now_iso()}",
    ]
    return "\n".join(parts)


def build_deal_comment(app: Application, enr: CompanyEnrichment) -> str:
    name = enr.name or app.applicant_name
    links = build_search_links(app.bin, name, enr.city or enr.region)
    return "\n".join(
        [
            "Новая заявка e-Qazyna",
            "",
            f"Номер заявки: {app.doc_number}",
            f"Дата создания заявки: {app.created_at_raw}",
            f"Тип документа: {app.doc_type}",
            f"Статус заявки: {app.status}",
            f"БИН: {app.bin}",
            f"Компания: {name}",
            f"Ключ заявки: {app.application_key}",
            f"Источник: {app.source_url}",
            "",
            "eGov:",
            f"Юридический адрес: {enr.legal_address or 'не найден'}",
            f"Регион: {enr.region or 'не определён'}",
            f"Город: {enr.city or 'не определён'}",
            f"Руководитель: {enr.director or 'не найден'}",
            f"Телефон из eGov: {enr.phone or 'не найден'}",
            f"ОКЭД / деятельность: {_activity_line(enr)}",
            f"Дата регистрации: {enr.registration_date or 'не найдена'}",
            "",
            "Ручной поиск контактов:",
            f"2GIS: {links['2gis']}",
            f"Google: {links['google']}",
            f"Yandex: {links['yandex']}",
        ]
    )


def build_deal_title(app: Application, enr: CompanyEnrichment) -> str:
    # Deal title must stay compact. The company is linked through COMPANY_ID
    # and shown in the Bitrix card separately; duplicating it in the deal
    # title makes the kanban unreadable.
    return f"e-Qazyna № {app.doc_number}"[:250]


def build_lead_title(app: Application, enr: CompanyEnrichment) -> str:
    name = enr.name or app.applicant_name
    return f"e-Qazyna лид — {name}"[:250]


def build_lead_comment(app: Application, enr: CompanyEnrichment, previous_comments: str | None = None) -> str:
    base = build_company_summary(app, enr)
    app_block = build_lead_application_block(app, enr)
    if previous_comments and app.application_key in previous_comments:
        return previous_comments
    if previous_comments:
        return f"{previous_comments.rstrip()}\n\n---\n{app_block}"[:65000]
    return f"{base}\n\n---\n{app_block}"[:65000]


def build_lead_application_block(app: Application, enr: CompanyEnrichment) -> str:
    return "\n".join(
        [
            "Заявка e-Qazyna в пакете лида",
            f"Номер заявки: {app.doc_number}",
            f"Дата создания заявки: {app.created_at_raw}",
            f"Тип документа: {app.doc_type}",
            f"Статус заявки: {app.status}",
            f"БИН: {app.bin}",
            f"Ключ заявки: {app.application_key}",
            f"Источник: {app.source_url}",
        ]
    )


def _activity_line(enr: CompanyEnrichment) -> str:
    if enr.oked and enr.activity:
        return f"{enr.oked} — {enr.activity}"
    return enr.activity or enr.oked or "не найдено"
