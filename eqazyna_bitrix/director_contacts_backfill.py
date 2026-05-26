from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .director import extract_director_from_text, split_director_fio
from .settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Bitrix contacts from 'Руководитель:' lines in e-Qazyna CRM comments")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--limit", type=int, default=int(os.getenv("DIRECTOR_BACKFILL_LIMIT", "0")), help="Max companies/deals per entity. 0 = no limit")
    parser.add_argument("--companies", action="store_true", help="Process companies")
    parser.add_argument("--deals", action="store_true", help="Process deals")
    parser.add_argument("--out", default="exports/director_contacts_backfill.json")
    parser.add_argument("--csv-out", default="exports/director_contacts_backfill.csv")
    parser.add_argument("--assigned-by-id", default=os.getenv("BITRIX_ASSIGNED_BY_ID") or None)
    return parser.parse_args()


def ensure_contact(
    client: BitrixClient,
    director_raw: str,
    company_id: str | None,
    deal_id: str | None,
    dry_run: bool,
    assigned_by_id: str | None = None,
) -> tuple[str | None, str, str | None]:
    fio = split_director_fio(director_raw)
    if not fio:
        return None, "director_parse_failed", None
    if dry_run:
        actions = ["dry_run_contact"]
        if company_id:
            actions.append("dry_run_link_company")
        if deal_id:
            actions.append("dry_run_link_deal")
        return "DRY_RUN_CONTACT", "+".join(actions), None
    try:
        contact = client.find_contact_by_fio(fio.last_name, fio.name, fio.second_name)
        if contact:
            contact_id = str(contact["ID"])
            actions = ["existing_contact"]
        else:
            fields: dict[str, Any] = {
                "LAST_NAME": fio.last_name,
                "NAME": fio.name,
                "SECOND_NAME": fio.second_name,
                "POST": "Руководитель",
                "SOURCE_ID": "OTHER",
                "SOURCE_DESCRIPTION": "eGov / e-Qazyna backfill",
                "COMMENTS": f"Руководитель извлечён из поля Дополнительно/COMMENTS. Исходное ФИО: {fio.raw}",
                "OPENED": "Y",
            }
            if company_id:
                fields["COMPANY_ID"] = int(company_id)
            if assigned_by_id:
                fields["ASSIGNED_BY_ID"] = int(assigned_by_id)
            contact_id = client.create_contact(fields)
            actions = ["created_contact"]
        if company_id:
            linked = client.link_contact_to_company(company_id, contact_id, primary=True)
            actions.append("linked_company" if linked else "company_already_linked")
        if deal_id:
            linked = client.link_contact_to_deal(deal_id, contact_id, primary=True)
            actions.append("linked_deal" if linked else "deal_already_linked")
        return contact_id, "+".join(actions), None
    except Exception as exc:  # noqa: BLE001
        return None, "error", str(exc)


def process_company(client: BitrixClient, company: dict[str, Any], dry_run: bool, assigned_by_id: str | None) -> dict[str, Any]:
    company_id = str(company.get("ID") or "")
    director = extract_director_from_text(company.get("COMMENTS"))
    if not director:
        return {"entity": "company", "entity_id": company_id, "title": company.get("TITLE"), "action": "director_not_found"}
    contact_id, action, error = ensure_contact(client, director, company_id, None, dry_run, assigned_by_id)
    return {
        "entity": "company",
        "entity_id": company_id,
        "title": company.get("TITLE"),
        "director": director,
        "contact_id": contact_id,
        "action": action,
        "error": error,
    }


def process_deal(client: BitrixClient, deal: dict[str, Any], dry_run: bool, assigned_by_id: str | None) -> dict[str, Any]:
    deal_id = str(deal.get("ID") or "")
    company_id = str(deal.get("COMPANY_ID") or "") or None
    director = extract_director_from_text(deal.get("COMMENTS"))
    if not director:
        return {"entity": "deal", "entity_id": deal_id, "company_id": company_id, "title": deal.get("TITLE"), "action": "director_not_found"}
    contact_id, action, error = ensure_contact(client, director, company_id, deal_id, dry_run, assigned_by_id)
    return {
        "entity": "deal",
        "entity_id": deal_id,
        "company_id": company_id,
        "title": deal.get("TITLE"),
        "director": director,
        "contact_id": contact_id,
        "action": action,
        "error": error,
    }


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")
    process_companies = args.companies or not args.deals
    process_deals = args.deals or not args.companies
    limit = args.limit or None
    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)
    rows: list[dict[str, Any]] = []
    if process_companies:
        companies = client.list_eqazyna_companies(limit=limit)
        print(f"Companies to scan: {len(companies)}")
        for idx, company in enumerate(companies, start=1):
            row = process_company(client, company, args.dry_run, args.assigned_by_id)
            print(f"company {idx}/{len(companies)} {row.get('entity_id')} {row.get('action')}")
            rows.append(row)
    if process_deals:
        deals = client.list_eqazyna_deals(limit=limit)
        print(f"Deals to scan: {len(deals)}")
        for idx, deal in enumerate(deals, start=1):
            row = process_deal(client, deal, args.dry_run, args.assigned_by_id)
            print(f"deal {idx}/{len(deals)} {row.get('entity_id')} {row.get('action')}")
            rows.append(row)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_out = Path(args.csv_out)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    keys = ["entity", "entity_id", "company_id", "title", "director", "contact_id", "action", "error"]
    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"JSON: {out}")
    print(f"CSV: {csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
