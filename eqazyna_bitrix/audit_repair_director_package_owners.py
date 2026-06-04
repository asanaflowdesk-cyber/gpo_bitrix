#!/usr/bin/env python3
"""Full audit and optional repair of owner consistency by director package.

Rule:
  one physical director + every linked director contact + every company in the
  director's deals + every deal of that director = one ASSIGNED_BY_ID.

The target owner is NOT selected by majority. It is the historical package
anchor: the manager on the oldest existing non-technical deal of the director.
Manual director fixations override the historical anchor. If no eligible deal
exists, the oldest eligible company owner is used as a fallback.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests

from .config.assignment import load_manual_director_owners_raw
from .director import (
    clean_director_value,
    director_identity_key,
    director_identity_keys,
    director_keys_match,
    extract_director_from_text,
)
from .distribute_companies import ALLOWED_USER_IDS

DEFAULT_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
ALLOWED_TARGET_IDS = {str(user_id) for user_id in ALLOWED_USER_IDS}


class BitrixError(RuntimeError):
    pass


def s(value: Any) -> str:
    return str(value or "").strip()


def parse_csv_set(text: str) -> Set[str]:
    return {x.strip() for x in (text or "").split(",") if x.strip()}


def normalize_webhook(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


def csv_write(path: str, rows: List[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def deal_url_hint(deal_id: Any) -> str:
    return f"/crm/deal/details/{deal_id}/" if deal_id else ""


def contact_url_hint(contact_id: Any) -> str:
    return f"/crm/contact/details/{contact_id}/" if contact_id else ""


def company_url_hint(company_id: Any) -> str:
    return f"/crm/company/details/{company_id}/" if company_id else ""


def parse_datetime(value: Any) -> datetime:
    raw = s(value)
    if not raw:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def entity_sort_key(record: Dict[str, Any]) -> Tuple[datetime, int]:
    try:
        entity_id = int(record.get("ID") or 0)
    except (TypeError, ValueError):
        entity_id = 0
    return parse_datetime(record.get("DATE_CREATE")), entity_id


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.session = requests.Session()
        self._user_cache: Dict[str, str] = {}
        self._contact_cache: Dict[str, Dict[str, Any]] = {}
        self._company_cache: Dict[str, Dict[str, Any]] = {}

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        response = self.session.post(self.base + method + ".json", json=payload or {}, timeout=self.timeout)
        try:
            data = response.json()
        except Exception as exc:
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:500]}") from exc
        if response.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:1200]}")
        return data.get("result")

    def list_all(self, method: str, payload: Optional[Dict[str, Any]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        base_payload = dict(payload or {})
        output: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            page_payload = dict(base_payload)
            page_payload["start"] = start
            result = self.call(method, page_payload)
            if isinstance(result, dict) and "items" in result:
                items = result.get("items") or []
                next_start = result.get("next")
            else:
                items = result if isinstance(result, list) else []
                next_start = start + 50 if len(items) >= 50 else None
            output.extend(items)
            if limit and len(output) >= limit:
                return output[:limit]
            if next_start is None or next_start == "":
                break
            start = next_start
            time.sleep(0.08)
        return output

    def get_user_name(self, user_id: Any) -> str:
        uid = s(user_id)
        if not uid:
            return ""
        if uid in self._user_cache:
            return self._user_cache[uid]
        name = f"ID {uid}"
        try:
            result = self.call("user.get", {"ID": uid})
            if isinstance(result, list) and result:
                user = result[0]
                full_name = " ".join(s(x) for x in [user.get("NAME"), user.get("LAST_NAME")] if s(x)).strip()
                name = full_name or s(user.get("EMAIL")) or name
        except Exception:
            pass
        self._user_cache[uid] = name
        return name

    def get_contact(self, contact_id: Any) -> Dict[str, Any]:
        cid = s(contact_id)
        if not cid:
            return {}
        if cid not in self._contact_cache:
            try:
                self._contact_cache[cid] = self.call("crm.contact.get", {"id": cid}) or {}
            except Exception:
                self._contact_cache[cid] = {}
        return self._contact_cache[cid]

    def get_company(self, company_id: Any) -> Dict[str, Any]:
        cid = s(company_id)
        if not cid:
            return {}
        if cid not in self._company_cache:
            try:
                self._company_cache[cid] = self.call("crm.company.get", {"id": cid}) or {}
            except Exception:
                self._company_cache[cid] = {}
        return self._company_cache[cid]

    def update_contact_owner(self, contact_id: str, owner_id: str) -> None:
        self.call("crm.contact.update", {"id": contact_id, "fields": {"ASSIGNED_BY_ID": owner_id}})
        self._contact_cache.pop(contact_id, None)

    def update_company_owner(self, company_id: str, owner_id: str) -> None:
        self.call("crm.company.update", {"id": company_id, "fields": {"ASSIGNED_BY_ID": owner_id}})
        self._company_cache.pop(company_id, None)

    def update_deal_owner(self, deal_id: str, owner_id: str) -> None:
        self.call("crm.deal.update", {"id": deal_id, "fields": {"ASSIGNED_BY_ID": owner_id}})


def contact_title(contact: Dict[str, Any]) -> str:
    parts = [contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME")]
    return " ".join(s(x) for x in parts if s(x)).strip() or s(contact.get("TITLE"))


def build_deal_filter(category_id: str, only_eqazyna: bool, include_closed: bool) -> Dict[str, Any]:
    deal_filter: Dict[str, Any] = {}
    if category_id and category_id.lower() != "all":
        deal_filter["CATEGORY_ID"] = category_id
    if only_eqazyna:
        deal_filter["ORIGINATOR_ID"] = "EQAZYNA"
    if not include_closed:
        deal_filter["CLOSED"] = "N"
    return deal_filter


def get_deals(bx: Bitrix, category_id: str, only_eqazyna: bool, include_closed: bool, max_deals: int) -> List[Dict[str, Any]]:
    select = [
        "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "COMMENTS",
        "STAGE_ID", "STAGE_SEMANTIC_ID", "CLOSED", "CATEGORY_ID", "ORIGINATOR_ID",
        "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY",
    ]
    payload = {
        "filter": build_deal_filter(category_id, only_eqazyna, include_closed),
        "select": select,
        "order": {"ID": "ASC"},
    }
    return bx.list_all("crm.deal.list", payload, limit=max_deals or None)


def load_manual_owner_index() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for user_id, director_names in load_manual_director_owners_raw().items():
        for director_name in director_names:
            for key in director_identity_keys(director_name) or [director_identity_key(director_name)]:
                if key:
                    result[key] = str(user_id)
    return result


def resolve_manual_owner(director_names: Sequence[str], manual_index: Dict[str, str]) -> str:
    owners: Set[str] = set()
    for name in director_names:
        for key in director_identity_keys(name) or [director_identity_key(name)]:
            if key and key in manual_index:
                owners.add(manual_index[key])
        for configured_key, owner_id in manual_index.items():
            configured_name = configured_key.split("|", 1)[-1]
            if director_keys_match(name, configured_name):
                owners.add(owner_id)
    if len(owners) == 1:
        return next(iter(owners))
    return ""


def resolve_director_for_deal(bx: Bitrix, deal: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Return director_key, director_name, contact_id, resolution_source."""
    contact_id = s(deal.get("CONTACT_ID"))
    if contact_id:
        contact = bx.get_contact(contact_id)
        name = clean_director_value(contact_title(contact))
        key = director_identity_key(name)
        if key:
            return key, name, contact_id, "contact"

    deal_name = extract_director_from_text(s(deal.get("COMMENTS")))
    deal_key = director_identity_key(deal_name)
    if deal_key:
        return deal_key, deal_name, contact_id, "deal_comments"

    company_id = s(deal.get("COMPANY_ID"))
    if company_id:
        company = bx.get_company(company_id)
        company_name = extract_director_from_text(s(company.get("COMMENTS")))
        company_key = director_identity_key(company_name)
        if company_key:
            return company_key, company_name, contact_id, "company_comments"

    return "", "", contact_id, "unresolved"


def eligible_owner(owner_id: str, source_ids: Set[str]) -> bool:
    return bool(owner_id and owner_id not in source_ids and owner_id in ALLOWED_TARGET_IDS)


def choose_historical_target(
    director_names: Sequence[str],
    package_deals: List[Dict[str, Any]],
    companies: Dict[str, Dict[str, Any]],
    contacts: Dict[str, Dict[str, Any]],
    source_ids: Set[str],
    manual_index: Dict[str, str],
) -> Tuple[str, str, str, Dict[str, Any]]:
    """Return target_id, reason, conflict_note, historical evidence."""
    manual_owner = resolve_manual_owner(director_names, manual_index)
    if manual_owner:
        return manual_owner, "manual_director_owner", "", {
            "historical_entity_type": "manual_director",
            "historical_entity_id": "",
            "historical_entity_date": "",
            "historical_owner_id": manual_owner,
        }

    eligible_deals = [deal for deal in package_deals if eligible_owner(s(deal.get("ASSIGNED_BY_ID")), source_ids)]
    if eligible_deals:
        oldest_deal = min(eligible_deals, key=entity_sort_key)
        owner_id = s(oldest_deal.get("ASSIGNED_BY_ID"))
        return owner_id, "historical_first_deal_owner", "", {
            "historical_entity_type": "deal",
            "historical_entity_id": s(oldest_deal.get("ID")),
            "historical_entity_date": s(oldest_deal.get("DATE_CREATE")),
            "historical_owner_id": owner_id,
        }

    eligible_companies = [company for company in companies.values() if eligible_owner(s(company.get("ASSIGNED_BY_ID")), source_ids)]
    if eligible_companies:
        oldest_company = min(eligible_companies, key=entity_sort_key)
        owner_id = s(oldest_company.get("ASSIGNED_BY_ID"))
        return owner_id, "historical_first_company_owner", "", {
            "historical_entity_type": "company",
            "historical_entity_id": s(oldest_company.get("ID")),
            "historical_entity_date": s(oldest_company.get("DATE_CREATE")),
            "historical_owner_id": owner_id,
        }

    contact_owners = sorted({s(contact.get("ASSIGNED_BY_ID")) for contact in contacts.values() if eligible_owner(s(contact.get("ASSIGNED_BY_ID")), source_ids)})
    if len(contact_owners) == 1:
        owner_id = contact_owners[0]
        return owner_id, "director_contact_owner_fallback", "", {
            "historical_entity_type": "contact_fallback",
            "historical_entity_id": "",
            "historical_entity_date": "",
            "historical_owner_id": owner_id,
        }
    if len(contact_owners) > 1:
        return "", "ambiguous_director_contacts", ",".join(contact_owners), {}

    return "", "no_historical_target_owner", "", {}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Full historical audit/repair of all existing Bitrix deals by director package")
    parser.add_argument("--source-responsible-ids", default="36,44")
    parser.add_argument("--deal-category-id", default="all")
    parser.add_argument("--target-policy", default="historical_first", help="Compatibility argument. Historical-first is always used.")
    parser.add_argument("--max-deals", type=int, default=0, help="0 = no limit")
    parser.add_argument("--summary-out", default="exports/director_package_owner_summary.csv")
    parser.add_argument("--mismatch-out", default="exports/director_package_owner_mismatches.csv")
    parser.add_argument("--unresolved-out", default="exports/director_package_owner_unresolved_deals.csv")
    parser.add_argument("--out", default="exports/director_package_owner_actions.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--only-eqazyna", action="store_true")
    parser.add_argument("--include-closed-deals", action="store_true")
    args = parser.parse_args(argv)

    bx = Bitrix(BITRIX_WEBHOOK_URL)
    source_ids = parse_csv_set(args.source_responsible_ids)
    manual_index = load_manual_owner_index()
    deals = get_deals(bx, args.deal_category_id, args.only_eqazyna, args.include_closed_deals, args.max_deals)

    packages: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "director_names": set(), "contact_ids": set(), "company_ids": set(), "deals": [], "resolution_sources": set()
    })
    unresolved_rows: List[Dict[str, Any]] = []

    for deal in deals:
        key, director_name, contact_id, source = resolve_director_for_deal(bx, deal)
        if not key:
            unresolved_rows.append({
                "deal_id": s(deal.get("ID")),
                "deal_title": s(deal.get("TITLE")),
                "company_id": s(deal.get("COMPANY_ID")),
                "contact_id": s(deal.get("CONTACT_ID")),
                "stage_id": s(deal.get("STAGE_ID")),
                "closed": s(deal.get("CLOSED")),
                "assigned_by_id": s(deal.get("ASSIGNED_BY_ID")),
                "assigned_by_name": bx.get_user_name(deal.get("ASSIGNED_BY_ID")),
                "reason": "director_not_resolved",
                "deal_url_hint": deal_url_hint(deal.get("ID")),
            })
            continue
        package = packages[key]
        package["director_names"].add(director_name)
        if contact_id:
            package["contact_ids"].add(contact_id)
        company_id = s(deal.get("COMPANY_ID"))
        if company_id:
            package["company_ids"].add(company_id)
        package["deals"].append(deal)
        package["resolution_sources"].add(source)

    summary_rows: List[Dict[str, Any]] = []
    mismatch_rows: List[Dict[str, Any]] = []
    action_rows: List[Dict[str, Any]] = []
    updated_contacts = updated_companies = updated_deals = error_count = 0

    for director_key, package in sorted(packages.items()):
        director_names = sorted(package["director_names"])
        contact_ids = sorted(package["contact_ids"], key=lambda value: int(value) if value.isdigit() else value)
        company_ids = sorted(package["company_ids"], key=lambda value: int(value) if value.isdigit() else value)
        package_deals = package["deals"]
        contacts = {cid: bx.get_contact(cid) for cid in contact_ids}
        companies = {cid: bx.get_company(cid) for cid in company_ids}

        target_id, target_reason, conflict_note, evidence = choose_historical_target(
            director_names, package_deals, companies, contacts, source_ids, manual_index
        )
        target_name = bx.get_user_name(target_id) if target_id else ""
        contact_owners = [s(contact.get("ASSIGNED_BY_ID")) for contact in contacts.values()]
        company_owners = [s(company.get("ASSIGNED_BY_ID")) for company in companies.values()]
        deal_owners = [s(deal.get("ASSIGNED_BY_ID")) for deal in package_deals]
        owner_set = {owner for owner in contact_owners + company_owners + deal_owners if owner}
        has_mismatch = len(owner_set) > 1 or bool(target_id and any(owner != target_id for owner in owner_set))

        summary_rows.append({
            "director_key": director_key,
            "director_names": " | ".join(director_names),
            "target_owner_id": target_id,
            "target_owner_name": target_name,
            "target_reason": target_reason,
            "historical_entity_type": evidence.get("historical_entity_type", ""),
            "historical_entity_id": evidence.get("historical_entity_id", ""),
            "historical_entity_date": evidence.get("historical_entity_date", ""),
            "historical_owner_id": evidence.get("historical_owner_id", ""),
            "historical_owner_name": bx.get_user_name(evidence.get("historical_owner_id")) if evidence.get("historical_owner_id") else "",
            "conflict_note": conflict_note,
            "contact_count": len(contact_ids),
            "company_count": len(company_ids),
            "deal_count": len(package_deals),
            "distinct_owner_ids": ",".join(sorted(owner_set, key=lambda value: int(value) if value.isdigit() else value)),
            "distinct_owner_names": "; ".join(bx.get_user_name(owner) for owner in sorted(owner_set, key=lambda value: int(value) if value.isdigit() else value)),
            "resolution_sources": ",".join(sorted(package["resolution_sources"])),
            "has_mismatch": "Y" if has_mismatch else "N",
        })

        def record_action(entity_type: str, entity_id: str, title: str, current_owner: str, url_hint: str, extra: Dict[str, Any]) -> None:
            nonlocal updated_contacts, updated_companies, updated_deals, error_count
            if not target_id or not current_owner or current_owner == target_id:
                return
            base = {
                "director_key": director_key,
                "director_names": " | ".join(director_names),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_title": title,
                "current_owner_id": current_owner,
                "current_owner_name": bx.get_user_name(current_owner),
                "target_owner_id": target_id,
                "target_owner_name": target_name,
                "target_reason": target_reason,
                "historical_entity_type": evidence.get("historical_entity_type", ""),
                "historical_entity_id": evidence.get("historical_entity_id", ""),
                "historical_entity_date": evidence.get("historical_entity_date", ""),
                "url_hint": url_hint,
                **extra,
            }
            mismatch_rows.append(base)
            status = "dry_run_update" if args.dry_run or not args.repair else "updated"
            error = ""
            if args.repair and not args.dry_run:
                try:
                    if entity_type == "contact":
                        bx.update_contact_owner(entity_id, target_id)
                        updated_contacts += 1
                    elif entity_type == "company":
                        bx.update_company_owner(entity_id, target_id)
                        updated_companies += 1
                    elif entity_type == "deal":
                        bx.update_deal_owner(entity_id, target_id)
                        updated_deals += 1
                except Exception as exc:
                    status = "error"
                    error = str(exc)
                    error_count += 1
            action_rows.append({
                "action": status,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_title": title,
                "from_owner_id": current_owner,
                "from_owner_name": bx.get_user_name(current_owner),
                "to_owner_id": target_id,
                "to_owner_name": target_name,
                "reason": target_reason,
                "historical_entity_type": evidence.get("historical_entity_type", ""),
                "historical_entity_id": evidence.get("historical_entity_id", ""),
                "historical_entity_date": evidence.get("historical_entity_date", ""),
                "error": error,
            })

        for contact_id, contact in contacts.items():
            record_action(
                "contact", contact_id, contact_title(contact), s(contact.get("ASSIGNED_BY_ID")), contact_url_hint(contact_id),
                {"company_id": "", "deal_id": "", "stage_id": "", "closed": ""},
            )
        for company_id, company in companies.items():
            record_action(
                "company", company_id, s(company.get("TITLE")) or company_id, s(company.get("ASSIGNED_BY_ID")), company_url_hint(company_id),
                {"company_id": company_id, "deal_id": "", "stage_id": "", "closed": ""},
            )
        for deal in package_deals:
            deal_id = s(deal.get("ID"))
            record_action(
                "deal", deal_id, s(deal.get("TITLE")) or deal_id, s(deal.get("ASSIGNED_BY_ID")), deal_url_hint(deal_id),
                {"company_id": s(deal.get("COMPANY_ID")), "deal_id": deal_id, "stage_id": s(deal.get("STAGE_ID")), "closed": s(deal.get("CLOSED"))},
            )

    summary_fields = [
        "director_key", "director_names", "target_owner_id", "target_owner_name", "target_reason",
        "historical_entity_type", "historical_entity_id", "historical_entity_date", "historical_owner_id", "historical_owner_name",
        "conflict_note", "contact_count", "company_count", "deal_count", "distinct_owner_ids", "distinct_owner_names",
        "resolution_sources", "has_mismatch",
    ]
    mismatch_fields = [
        "director_key", "director_names", "entity_type", "entity_id", "entity_title", "current_owner_id", "current_owner_name",
        "target_owner_id", "target_owner_name", "target_reason", "historical_entity_type", "historical_entity_id", "historical_entity_date",
        "company_id", "deal_id", "stage_id", "closed", "url_hint",
    ]
    unresolved_fields = [
        "deal_id", "deal_title", "company_id", "contact_id", "stage_id", "closed", "assigned_by_id", "assigned_by_name", "reason", "deal_url_hint",
    ]
    action_fields = [
        "action", "entity_type", "entity_id", "entity_title", "from_owner_id", "from_owner_name", "to_owner_id", "to_owner_name",
        "reason", "historical_entity_type", "historical_entity_id", "historical_entity_date", "error",
    ]

    csv_write(args.summary_out, summary_rows, summary_fields)
    csv_write(args.mismatch_out, mismatch_rows, mismatch_fields)
    csv_write(args.unresolved_out, unresolved_rows, unresolved_fields)
    csv_write(args.out, action_rows, action_fields)

    print("HISTORICAL_DIRECTOR_PACKAGE_OWNER_AUDIT_DONE")
    print(json.dumps({
        "deals_loaded": len(deals),
        "packages": len(packages),
        "unresolved_deals": len(unresolved_rows),
        "summary_rows": len(summary_rows),
        "mismatch_rows": len(mismatch_rows),
        "action_rows": len(action_rows),
        "dry_run": bool(args.dry_run),
        "repair": bool(args.repair),
        "updated_contacts": updated_contacts,
        "updated_companies": updated_companies,
        "updated_deals": updated_deals,
        "errors": error_count,
    }, ensure_ascii=False, indent=2))
    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
