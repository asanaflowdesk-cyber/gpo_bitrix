#!/usr/bin/env python3
"""Audit and optionally repair Bitrix owner consistency by director package.

Package rule:
  director contact + all companies in this director's deals + all deals of this director
  must have the same ASSIGNED_BY_ID.

This script is intentionally self-contained and uses Bitrix REST directly.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests


DEFAULT_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").strip()


class BitrixError(RuntimeError):
    pass


def normalize_webhook(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.session = requests.Session()
        self._user_cache: Dict[str, str] = {}
        self._contact_cache: Dict[str, Dict[str, Any]] = {}
        self._company_cache: Dict[str, Dict[str, Any]] = {}

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + method + ".json"
        resp = self.session.post(url, json=payload or {}, timeout=self.timeout)
        try:
            data = resp.json()
        except Exception:
            raise BitrixError(f"{method}: HTTP {resp.status_code}: {resp.text[:500]}")
        if resp.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:1200]}")
        return data.get("result")

    def list_all(self, method: str, payload: Optional[Dict[str, Any]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        payload = dict(payload or {})
        out: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            p = dict(payload)
            p["start"] = start
            result = self.call(method, p)
            if isinstance(result, dict) and "items" in result:
                items = result.get("items") or []
                next_start = result.get("next")
            else:
                items = result if isinstance(result, list) else []
                # Bitrix puts next outside result only for GET-like JSON, but with POST wrapper it is not always returned.
                # Safe fallback: if less than 50, stop. If exactly 50, increment.
                next_start = start + 50 if len(items) >= 50 else None
            out.extend(items)
            if limit and len(out) >= limit:
                return out[:limit]
            if next_start is None or next_start == "":
                break
            start = next_start
            time.sleep(0.08)
        return out

    def get_user_name(self, user_id: Any) -> str:
        uid = str(user_id or "").strip()
        if not uid:
            return ""
        if uid in self._user_cache:
            return self._user_cache[uid]
        name = f"ID {uid}"
        try:
            result = self.call("user.get", {"ID": uid})
            if isinstance(result, list) and result:
                u = result[0]
                parts = [u.get("NAME"), u.get("LAST_NAME")]
                email = u.get("EMAIL")
                full = " ".join(str(x).strip() for x in parts if str(x or "").strip()).strip()
                name = full or email or name
        except Exception:
            pass
        self._user_cache[uid] = name
        return name

    def get_contact(self, contact_id: Any) -> Dict[str, Any]:
        cid = str(contact_id or "").strip()
        if not cid:
            return {}
        if cid not in self._contact_cache:
            try:
                self._contact_cache[cid] = self.call("crm.contact.get", {"id": cid}) or {}
            except Exception:
                self._contact_cache[cid] = {}
        return self._contact_cache[cid]

    def get_company(self, company_id: Any) -> Dict[str, Any]:
        cid = str(company_id or "").strip()
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
        self._contact_cache.pop(str(contact_id), None)

    def update_company_owner(self, company_id: str, owner_id: str) -> None:
        self.call("crm.company.update", {"id": company_id, "fields": {"ASSIGNED_BY_ID": owner_id}})
        self._company_cache.pop(str(company_id), None)

    def update_deal_owner(self, deal_id: str, owner_id: str) -> None:
        self.call("crm.deal.update", {"id": deal_id, "fields": {"ASSIGNED_BY_ID": owner_id}})


def s(value: Any) -> str:
    return str(value or "").strip()


def parse_csv_set(text: str) -> Set[str]:
    return {x.strip() for x in (text or "").split(",") if x.strip()}


def csv_write(path: str, rows: List[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(fieldnames), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def deal_url_hint(deal_id: Any) -> str:
    return f"/crm/deal/details/{deal_id}/" if deal_id else ""


def contact_url_hint(contact_id: Any) -> str:
    return f"/crm/contact/details/{contact_id}/" if contact_id else ""


def company_url_hint(company_id: Any) -> str:
    return f"/crm/company/details/{company_id}/" if company_id else ""


def contact_title(c: Dict[str, Any]) -> str:
    parts = [c.get("LAST_NAME"), c.get("NAME"), c.get("SECOND_NAME")]
    title = " ".join(str(x).strip() for x in parts if str(x or "").strip()).strip()
    return title or s(c.get("TITLE")) or s(c.get("ID"))


def build_deal_filter(category_id: str, only_eqazyna: bool, include_closed: bool) -> Dict[str, Any]:
    flt: Dict[str, Any] = {}
    if category_id and category_id.lower() != "all":
        flt["CATEGORY_ID"] = category_id
    if only_eqazyna:
        flt["ORIGINATOR_ID"] = "EQAZYNA"
    if not include_closed:
        flt["CLOSED"] = "N"
    return flt


def get_deals(bx: Bitrix, category_id: str, only_eqazyna: bool, include_closed: bool, max_deals: int) -> List[Dict[str, Any]]:
    select = [
        "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID",
        "STAGE_ID", "STAGE_SEMANTIC_ID", "CLOSED", "CATEGORY_ID",
        "ORIGINATOR_ID", "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY",
    ]
    payload = {"filter": build_deal_filter(category_id, only_eqazyna, include_closed), "select": select, "order": {"ID": "ASC"}}
    return bx.list_all("crm.deal.list", payload, limit=max_deals or None)


def choose_target_owner(contact_owner: str, deal_owners: List[str], company_owners: List[str], source_ids: Set[str], policy: str) -> Tuple[str, str, str]:
    """Return target_id, target_reason, conflict_note."""
    policy = (policy or "rules_then_majority").strip().lower()
    valid_deal_owners = [o for o in deal_owners if o and o not in source_ids]
    valid_company_owners = [o for o in company_owners if o and o not in source_ids]

    if policy in {"director", "contact", "director_contact"}:
        if contact_owner:
            return contact_owner, "director_contact_owner", ""
        return "", "no_director_owner", ""

    # Rule 1: non-technical director owner is strongest signal.
    if contact_owner and contact_owner not in source_ids:
        return contact_owner, "director_contact_owner", ""

    # Rule 2: all non-technical deals already agree.
    uniq_deals = sorted(set(valid_deal_owners))
    if len(uniq_deals) == 1:
        return uniq_deals[0], "single_non_technical_deal_owner", ""

    # Rule 3: all non-technical companies already agree.
    uniq_companies = sorted(set(valid_company_owners))
    if len(uniq_companies) == 1 and not uniq_deals:
        return uniq_companies[0], "single_non_technical_company_owner", ""

    # Rule 4: weighted majority, deals weigh more than companies/contact.
    weights: Counter[str] = Counter()
    for o in valid_deal_owners:
        weights[o] += 3
    for o in valid_company_owners:
        weights[o] += 1
    if contact_owner and contact_owner not in source_ids:
        weights[contact_owner] += 5

    if weights:
        top = weights.most_common()
        if len(top) == 1 or top[0][1] > top[1][1]:
            return top[0][0], "weighted_majority", ""
        return "", "ambiguous_majority", "; ".join(f"{k}:{v}" for k, v in top)

    if contact_owner:
        return contact_owner, "technical_director_owner_only", ""
    return "", "no_target_owner", ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit/repair Bitrix director package owner consistency")
    parser.add_argument("--source-responsible-ids", default="36,44", help="Technical/source owner IDs, comma-separated")
    parser.add_argument("--deal-category-id", default="all", help="Deal category ID or all")
    parser.add_argument("--target-policy", default="rules_then_majority", help="rules_then_majority/director")
    parser.add_argument("--max-deals", type=int, default=0, help="Limit deals for diagnostics; 0 = no limit")
    parser.add_argument("--summary-out", default="exports/director_package_owner_summary.csv")
    parser.add_argument("--mismatch-out", default="exports/director_package_owner_mismatches.csv")
    parser.add_argument("--out", default="exports/director_package_owner_actions.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--only-eqazyna", action="store_true")
    parser.add_argument("--include-closed-deals", action="store_true")
    args = parser.parse_args(argv)

    bx = Bitrix(BITRIX_WEBHOOK_URL)
    source_ids = parse_csv_set(args.source_responsible_ids)

    deals = get_deals(
        bx=bx,
        category_id=args.deal_category_id,
        only_eqazyna=args.only_eqazyna,
        include_closed=args.include_closed_deals,
        max_deals=args.max_deals,
    )

    packages: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    skipped_no_contact = 0
    for d in deals:
        contact_id = s(d.get("CONTACT_ID"))
        if not contact_id:
            skipped_no_contact += 1
            continue
        packages[contact_id].append(d)

    summary_rows: List[Dict[str, Any]] = []
    mismatch_rows: List[Dict[str, Any]] = []
    action_rows: List[Dict[str, Any]] = []

    updated_contacts = updated_companies = updated_deals = 0
    error_count = 0

    for contact_id, pkg_deals in sorted(packages.items(), key=lambda x: int(x[0]) if x[0].isdigit() else x[0]):
        contact = bx.get_contact(contact_id)
        director_owner = s(contact.get("ASSIGNED_BY_ID"))
        director_name = contact_title(contact)

        company_ids = sorted({s(d.get("COMPANY_ID")) for d in pkg_deals if s(d.get("COMPANY_ID"))}, key=lambda x: int(x) if x.isdigit() else x)
        companies = {cid: bx.get_company(cid) for cid in company_ids}

        deal_owners = [s(d.get("ASSIGNED_BY_ID")) for d in pkg_deals]
        company_owners = [s(c.get("ASSIGNED_BY_ID")) for c in companies.values()]
        target_id, target_reason, conflict_note = choose_target_owner(director_owner, deal_owners, company_owners, source_ids, args.target_policy)
        target_name = bx.get_user_name(target_id) if target_id else ""

        owner_set = {o for o in ([director_owner] + deal_owners + company_owners) if o}
        mismatched = bool(target_id and any(o and o != target_id for o in owner_set)) or len(owner_set) > 1

        summary_rows.append({
            "director_contact_id": contact_id,
            "director_fio": director_name,
            "director_owner_id": director_owner,
            "director_owner_name": bx.get_user_name(director_owner),
            "target_owner_id": target_id,
            "target_owner_name": target_name,
            "target_reason": target_reason,
            "conflict_note": conflict_note,
            "deal_count": len(pkg_deals),
            "company_count": len(company_ids),
            "distinct_owner_ids": ",".join(sorted(owner_set, key=lambda x: int(x) if x.isdigit() else x)),
            "distinct_owner_names": "; ".join(bx.get_user_name(o) for o in sorted(owner_set, key=lambda x: int(x) if x.isdigit() else x)),
            "has_mismatch": "Y" if mismatched else "N",
            "contact_url_hint": contact_url_hint(contact_id),
        })

        # Contact mismatch/action
        if target_id and director_owner and director_owner != target_id:
            mismatch_rows.append({
                "entity_type": "contact",
                "entity_id": contact_id,
                "entity_title": director_name,
                "current_owner_id": director_owner,
                "current_owner_name": bx.get_user_name(director_owner),
                "target_owner_id": target_id,
                "target_owner_name": target_name,
                "director_contact_id": contact_id,
                "director_fio": director_name,
                "company_id": "",
                "company_title": "",
                "deal_id": "",
                "deal_title": "",
                "stage_id": "",
                "closed": "",
                "url_hint": contact_url_hint(contact_id),
            })
            status = "dry_run_update" if args.dry_run or not args.repair else "updated"
            err = ""
            if args.repair and not args.dry_run:
                try:
                    bx.update_contact_owner(contact_id, target_id)
                    updated_contacts += 1
                except Exception as e:
                    status = "error"
                    err = str(e)
                    error_count += 1
            action_rows.append({
                "action": status,
                "entity_type": "contact",
                "entity_id": contact_id,
                "entity_title": director_name,
                "from_owner_id": director_owner,
                "from_owner_name": bx.get_user_name(director_owner),
                "to_owner_id": target_id,
                "to_owner_name": target_name,
                "reason": target_reason,
                "error": err,
            })

        # Company mismatches/actions
        for cid, comp in companies.items():
            owner = s(comp.get("ASSIGNED_BY_ID"))
            title = s(comp.get("TITLE")) or s(comp.get("COMPANY_TITLE")) or cid
            if target_id and owner and owner != target_id:
                mismatch_rows.append({
                    "entity_type": "company",
                    "entity_id": cid,
                    "entity_title": title,
                    "current_owner_id": owner,
                    "current_owner_name": bx.get_user_name(owner),
                    "target_owner_id": target_id,
                    "target_owner_name": target_name,
                    "director_contact_id": contact_id,
                    "director_fio": director_name,
                    "company_id": cid,
                    "company_title": title,
                    "deal_id": "",
                    "deal_title": "",
                    "stage_id": "",
                    "closed": "",
                    "url_hint": company_url_hint(cid),
                })
                status = "dry_run_update" if args.dry_run or not args.repair else "updated"
                err = ""
                if args.repair and not args.dry_run:
                    try:
                        bx.update_company_owner(cid, target_id)
                        updated_companies += 1
                    except Exception as e:
                        status = "error"
                        err = str(e)
                        error_count += 1
                action_rows.append({
                    "action": status,
                    "entity_type": "company",
                    "entity_id": cid,
                    "entity_title": title,
                    "from_owner_id": owner,
                    "from_owner_name": bx.get_user_name(owner),
                    "to_owner_id": target_id,
                    "to_owner_name": target_name,
                    "reason": target_reason,
                    "error": err,
                })

        # Deal mismatches/actions
        for d in pkg_deals:
            deal_id = s(d.get("ID"))
            owner = s(d.get("ASSIGNED_BY_ID"))
            title = s(d.get("TITLE")) or deal_id
            cid = s(d.get("COMPANY_ID"))
            comp_title = s(companies.get(cid, {}).get("TITLE")) if cid else ""
            if target_id and owner and owner != target_id:
                mismatch_rows.append({
                    "entity_type": "deal",
                    "entity_id": deal_id,
                    "entity_title": title,
                    "current_owner_id": owner,
                    "current_owner_name": bx.get_user_name(owner),
                    "target_owner_id": target_id,
                    "target_owner_name": target_name,
                    "director_contact_id": contact_id,
                    "director_fio": director_name,
                    "company_id": cid,
                    "company_title": comp_title,
                    "deal_id": deal_id,
                    "deal_title": title,
                    "stage_id": s(d.get("STAGE_ID")),
                    "closed": s(d.get("CLOSED")),
                    "url_hint": deal_url_hint(deal_id),
                })
                status = "dry_run_update" if args.dry_run or not args.repair else "updated"
                err = ""
                if args.repair and not args.dry_run:
                    try:
                        bx.update_deal_owner(deal_id, target_id)
                        updated_deals += 1
                    except Exception as e:
                        status = "error"
                        err = str(e)
                        error_count += 1
                action_rows.append({
                    "action": status,
                    "entity_type": "deal",
                    "entity_id": deal_id,
                    "entity_title": title,
                    "from_owner_id": owner,
                    "from_owner_name": bx.get_user_name(owner),
                    "to_owner_id": target_id,
                    "to_owner_name": target_name,
                    "reason": target_reason,
                    "error": err,
                })

    summary_fields = [
        "director_contact_id", "director_fio", "director_owner_id", "director_owner_name",
        "target_owner_id", "target_owner_name", "target_reason", "conflict_note",
        "deal_count", "company_count", "distinct_owner_ids", "distinct_owner_names",
        "has_mismatch", "contact_url_hint",
    ]
    mismatch_fields = [
        "entity_type", "entity_id", "entity_title", "current_owner_id", "current_owner_name",
        "target_owner_id", "target_owner_name", "director_contact_id", "director_fio",
        "company_id", "company_title", "deal_id", "deal_title", "stage_id", "closed", "url_hint",
    ]
    action_fields = [
        "action", "entity_type", "entity_id", "entity_title", "from_owner_id", "from_owner_name",
        "to_owner_id", "to_owner_name", "reason", "error",
    ]

    csv_write(args.summary_out, summary_rows, summary_fields)
    csv_write(args.mismatch_out, mismatch_rows, mismatch_fields)
    csv_write(args.out, action_rows, action_fields)

    print("DIRECTOR_PACKAGE_OWNER_AUDIT_DONE")
    print(json.dumps({
        "deals_loaded": len(deals),
        "packages": len(packages),
        "skipped_deals_without_contact": skipped_no_contact,
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
