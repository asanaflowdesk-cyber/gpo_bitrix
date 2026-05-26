from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .settings import Settings


LEAD_ORIGINATOR_ID = "EQAZYNA_DIRECTOR_LEAD"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone existing e-Qazyna companies/deal packages into Bitrix leads grouped by director or company"
    )
    parser.add_argument("--limit", type=int, default=0, help="Max companies to process. 0 = no limit")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--lead-status-id", default="NEW", help="Lead status ID for created/updated leads")
    parser.add_argument("--assigned-by-id", default=None, help="Optional responsible user ID for cloned leads")
    parser.add_argument("--out", default=None, help="Output JSON log path")
    parser.add_argument(
        "--include-non-eqazyna-deals",
        action="store_true",
        help="Include all company deals, not only ORIGINATOR_ID=EQAZYNA",
    )
    return parser.parse_args()


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize_text(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^\w\sа-яА-Яa-zA-Z0-9әғқңөұүһіӘҒҚҢӨҰҮҺІ]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_director_from_comments(comments: str) -> str:
    """
    Пытаемся достать руководителя из комментариев компании.

    Если в поле написано "не найден", "не указан", "нет данных" и т.п.,
    считаем, что руководителя нет, и дальше создаём лид по компании.
    """
    if not comments:
        return ""

    patterns = [
        r"(?:первый\s+руководитель|руководитель|директор)\s*[:\-]\s*(.+)",
        r"(?:фио\s+руководителя)\s*[:\-]\s*(.+)",
    ]

    invalid_values = {
        "не найден",
        "не найдена",
        "не найдено",
        "не указан",
        "не указана",
        "не указано",
        "нет",
        "нет данных",
        "отсутствует",
        "n/a",
        "na",
        "-",
        "—",
    }

    for pattern in patterns:
        match = re.search(pattern, comments, flags=re.IGNORECASE)
        if match:
            director = match.group(1).strip()
            director = director.split("\n")[0].strip()
            director = re.sub(r"\s{2,}", " ", director)

            normalized = _normalize_text(director)

            if not normalized:
                return ""

            if normalized in invalid_values:
                return ""

            return director[:255]

    return ""

def _company_bin(company: dict[str, Any]) -> str:
    return _safe_str(company.get("ORIGIN_ID")).strip()


def _company_title(company: dict[str, Any]) -> str:
    return _safe_str(company.get("TITLE") or "Компания без названия").strip()


def _director_name(company: dict[str, Any]) -> str:
    comments = _safe_str(company.get("COMMENTS"))
    return _extract_director_from_comments(comments)


def _group_key(company: dict[str, Any]) -> tuple[str, str, str, str]:
    """
    Возвращает:
    - group_key: ключ для группировки внутри скрипта
    - origin_id: ключ лида в Bitrix
    - readable_name: человекочитаемое имя для заголовка
    - group_type: director или company

    Правило:
    - если руководитель найден → группируем по руководителю;
    - если руководитель не найден → создаём отдельный лид по компании.
    """
    director = _director_name(company)

    if director:
        normalized_director = _normalize_text(director)
        return (
            f"director|{normalized_director}",
            f"director|{normalized_director}",
            director,
            "director",
        )

    bin_value = _company_bin(company) or f"company-{company.get('ID')}"
    title = _company_title(company)

    return (
        f"company|{bin_value}",
        f"company|{bin_value}",
        title,
        "company",
    )


def _lead_title(readable_name: str, companies: list[dict[str, Any]], group_type: str) -> str:
    if group_type == "company":
        return f"e-Qazyna пакет компании — {readable_name}"[:250]

    return f"e-Qazyna пакет руководителя — {readable_name} ({len(companies)} комп.)"[:250]


def _package_comment(
    readable_name: str,
    group_type: str,
    companies: list[dict[str, Any]],
    deals_by_company: dict[str, list[dict[str, Any]]],
) -> str:
    if group_type == "director":
        group_line = f"Руководитель / ключ группы: {readable_name}"
        grouping_line = "Группировка выполнена по руководителю."
    else:
        group_line = f"Компания / ключ группы: {readable_name}"
        grouping_line = "Руководитель не найден. Лид создан отдельно по компании."

    parts = [
        "Лид создан из существующих компаний Bitrix24.",
        grouping_line,
        "Это миграционный лид для первичной обработки, а не подтверждённый клиент.",
        "",
        group_line,
        f"Количество компаний в пакете: {len(companies)}",
        "",
        "Компании и сделки в пакете:",
    ]

    total_deals = 0

    for company in companies:
        company_id = str(company.get("ID"))
        deals = deals_by_company.get(company_id, [])
        total_deals += len(deals)

        director = _director_name(company)

        parts += [
            "",
            f"Компания Bitrix ID: {company.get('ID')}",
            f"Компания: {_company_title(company)}",
            f"БИН / ключ компании: {_safe_str(company.get('ORIGIN_ID'))}",
            f"Руководитель: {director if director else 'не найден'}",
            f"Ответственный компании до миграции: {_safe_str(company.get('ASSIGNED_BY_ID'))}",
            f"Сделок в компании: {len(deals)}",
        ]

        if not deals:
            parts.append("- Сделки не найдены")
        else:
            for deal in deals:
                parts.append(
                    "- "
                    f"Deal ID {deal.get('ID')} | "
                    f"{_safe_str(deal.get('TITLE'))} | "
                    f"Стадия: {_safe_str(deal.get('STAGE_ID'))} | "
                    f"Воронка: {_safe_str(deal.get('CATEGORY_ID'))} | "
                    f"Закрыта: {_safe_str(deal.get('CLOSED'))} | "
                    f"Ключ: {_safe_str(deal.get('ORIGIN_ID'))}"
                )

    parts.insert(6, f"Всего сделок в пакете: {total_deals}")

    return "\n".join(parts)[:65000]


def _choose_responsible(companies: list[dict[str, Any]], assigned_by_id: str | None) -> int | None:
    if assigned_by_id:
        try:
            return int(assigned_by_id)
        except (TypeError, ValueError):
            return None

    for company in companies:
        responsible = company.get("ASSIGNED_BY_ID")
        if responsible:
            try:
                return int(responsible)
            except (TypeError, ValueError):
                continue

    return None


def _lead_fields(
    origin_id: str,
    readable_name: str,
    group_type: str,
    companies: list[dict[str, Any]],
    deals_by_company: dict[str, list[dict[str, Any]]],
    lead_status_id: str,
    assigned_by_id: str | None,
) -> dict[str, Any]:
    first_company = companies[0]

    fields: dict[str, Any] = {
        "TITLE": _lead_title(readable_name, companies, group_type),
        "COMPANY_TITLE": _company_title(first_company)[:255],
        "STATUS_ID": lead_status_id or "NEW",
        "OPENED": "Y",
        "COMMENTS": _package_comment(readable_name, group_type, companies, deals_by_company),
        "ORIGINATOR_ID": LEAD_ORIGINATOR_ID,
        "ORIGIN_ID": origin_id,
        "SOURCE_ID": "OTHER",
        "SOURCE_DESCRIPTION": "Migration: e-Qazyna company/deal package to lead grouped by director or company",
    }

    responsible = _choose_responsible(companies, assigned_by_id)
    if responsible:
        fields["ASSIGNED_BY_ID"] = responsible

    return fields


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()

    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")

    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)
    limit = args.limit if args.limit and args.limit > 0 else None

    companies = client.list_eqazyna_companies(limit=limit)
    print(f"Found e-Qazyna companies: {len(companies)}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_meta: dict[str, dict[str, str]] = {}

    for company in companies:
        group_key, origin_id, readable_name, group_type = _group_key(company)

        groups[group_key].append(company)
        group_meta[group_key] = {
            "origin_id": origin_id,
            "readable_name": readable_name,
            "group_type": group_type,
        }

    print(f"Lead groups found: {len(groups)}")

    results: list[dict[str, Any]] = []

    for idx, (group_key, group_companies) in enumerate(groups.items(), start=1):
        meta = group_meta[group_key]
        origin_id = meta["origin_id"]
        readable_name = meta["readable_name"]
        group_type = meta["group_type"]

        print(
            f"{idx}/{len(groups)} "
            f"type={group_type} "
            f"group={origin_id} "
            f"name={readable_name[:80]} "
            f"companies={len(group_companies)}"
        )

        try:
            deals_by_company: dict[str, list[dict[str, Any]]] = {}
            total_deals = 0

            for company in group_companies:
                company_id = str(company.get("ID"))

                deals = client.list_deals_by_company(
                    company_id,
                    only_eqazyna=not args.include_non_eqazyna_deals,
                )

                deals_by_company[company_id] = deals
                total_deals += len(deals)

            existing = client.find_lead_by_origin(
                origin_id,
                originator_id=LEAD_ORIGINATOR_ID,
            )

            fields = _lead_fields(
                origin_id=origin_id,
                readable_name=readable_name,
                group_type=group_type,
                companies=group_companies,
                deals_by_company=deals_by_company,
                lead_status_id=args.lead_status_id,
                assigned_by_id=args.assigned_by_id,
            )

            if args.dry_run:
                if existing:
                    action = "dry_run_update_lead"
                    lead_id = str(existing.get("ID"))
                else:
                    action = "dry_run_create_lead"
                    lead_id = "DRY_RUN"

            elif existing:
                lead_id = str(existing["ID"])
                client.update_lead(lead_id, fields)
                action = "updated_lead_from_package"

            else:
                lead_id = client.create_lead(fields)
                action = "created_lead_from_package"

            results.append(
                {
                    "group_key": group_key,
                    "origin_id": origin_id,
                    "group_type": group_type,
                    "director": readable_name if group_type == "director" else None,
                    "company_lead_title": readable_name if group_type == "company" else None,
                    "lead_id": lead_id,
                    "company_count": len(group_companies),
                    "deal_count": total_deals,
                    "companies": [
                        {
                            "company_id": company.get("ID"),
                            "company_title": company.get("TITLE"),
                            "bin": company.get("ORIGIN_ID"),
                            "director": _director_name(company) or None,
                        }
                        for company in group_companies
                    ],
                    "action": action,
                    "error": None,
                }
            )

            print(
                f"  {action}: lead={lead_id}, "
                f"type={group_type}, "
                f"companies={len(group_companies)}, "
                f"deals={total_deals}"
            )

        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "group_key": group_key,
                    "origin_id": origin_id,
                    "group_type": group_type,
                    "director": readable_name if group_type == "director" else None,
                    "company_lead_title": readable_name if group_type == "company" else None,
                    "lead_id": None,
                    "company_count": len(group_companies),
                    "deal_count": None,
                    "companies": [
                        {
                            "company_id": company.get("ID"),
                            "company_title": company.get("TITLE"),
                            "bin": company.get("ORIGIN_ID"),
                            "director": _director_name(company) or None,
                        }
                        for company in group_companies
                    ],
                    "action": "error",
                    "error": str(exc),
                }
            )

            print(f"  ERROR: {exc}")

    out = Path(args.out or f"exports/clients_to_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"JSON: {out}")
    print("Done")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
