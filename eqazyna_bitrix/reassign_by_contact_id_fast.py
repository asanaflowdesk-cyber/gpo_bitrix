#!/usr/bin/env python3
"""Fast Bitrix CRM reassignment by exact contact ID.

Moves only the exact package anchored by a contact ID:
- source contact;
- companies linked to the contact;
- deals linked to the contact or to those companies.

This version intentionally avoids slow per-deal contact expansion and delayed verification.
It verifies each write immediately and logs the actual owner after update.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off", ""}

ENTITY_METHODS = {
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update", "list": "crm.contact.list"},
    "company": {"get": "crm.company.get", "update": "crm.company.update", "list": "crm.company.list"},
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update", "list": "crm.deal.list"},
}

SELECT_FIELDS = {
    "contact": ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "FULL_NAME", "ASSIGNED_BY_ID", "COMPANY_ID"],
    "company": ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID"],
    "deal": [
        "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "ORIGINATOR_ID", "ORIGIN_ID",
        "CATEGORY_ID", "STAGE_ID", "CLOSED",
    ],
}


class BitrixError(RuntimeError):
    pass


def text(value: Any) -> str:
    return str(value or "").strip()


def norm_id(value: Any) -> str:
    raw = text(value)
    if not raw:
        return ""
    try:
        return str(int(raw))
    except Exception:
        return raw


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    raw = text(value).lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def normalize_webhook(url: str) -> str:
    url = text(url)
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


def title_of(entity_type: str, row: Dict[str, Any]) -> str:
    if entity_type in {"company", "deal"}:
        return text(row.get("TITLE"))
    parts = [text(row.get("LAST_NAME")), text(row.get("NAME")), text(row.get("SECOND_NAME"))]
    return " ".join(part for part in parts if part).strip() or text(row.get("FULL_NAME")) or f"Contact #{row.get('ID')}"


def add_select(params: List[Tuple[str, Any]], fields: Iterable[str]) -> None:
    for field in fields:
        params.append(("select[]", field))


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = 60, sleep_seconds: float = 0.05) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()

    def _parse_response(self, method: str, response: requests.Response) -> Dict[str, Any]:
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        try:
            data = response.json()
        except Exception as exc:
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:1000]}") from exc
        if response.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:2000]}")
        return data

    def call_json_full(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(self.base + method + ".json", json=payload or {}, timeout=self.timeout)
                return self._parse_response(method, response)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3 and ("QUERY_LIMIT" in str(exc) or "TOO_MANY" in str(exc) or "HTTP 429" in str(exc)):
                    time.sleep(attempt)
                    continue
                raise
        raise BitrixError(str(last_error))

    def call_json(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self.call_json_full(method, payload).get("result")

    def call_form_full(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(self.base + method + ".json", data=list(params or []), timeout=self.timeout)
                return self._parse_response(method, response)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3 and ("QUERY_LIMIT" in str(exc) or "TOO_MANY" in str(exc) or "HTTP 429" in str(exc)):
                    time.sleep(attempt)
                    continue
                raise
        raise BitrixError(str(last_error))

    def call_form(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Any:
        return self.call_form_full(method, params).get("result")

    def list_all(self, method: str, params_base: List[Tuple[str, Any]], limit: int = 0) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            params = list(params_base)
            params.append(("start", start))
            payload = self.call_form_full(method, params)
            result = payload.get("result")
            if isinstance(result, dict) and "items" in result:
                items = result.get("items") or []
                next_start = result.get("next") or payload.get("next")
            else:
                items = result or []
                next_start = payload.get("next")
            for item in items:
                if isinstance(item, dict):
                    rows.append(dict(item))
                    if limit and len(rows) >= limit:
                        return rows
            if next_start in (None, "", False):
                break
            start = next_start
        return rows

    def validate_user(self, user_id: str) -> str:
        result = self.call_json("user.get", {"ID": user_id})
        user = dict(result[0]) if isinstance(result, list) and result else {}
        if not user:
            raise ValueError(f"target_user_id={user_id} not found by user.get")
        active = text(user.get("ACTIVE")).lower()
        if active in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={user_id} is inactive")
        return " ".join(text(x) for x in [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")] if text(x)).strip() or text(user.get("EMAIL")) or f"ID {user_id}"

    def get_entity(self, entity_type: str, entity_id: str) -> Dict[str, Any]:
        result = self.call_json(ENTITY_METHODS[entity_type]["get"], {"id": entity_id})
        if not isinstance(result, dict) or not result:
            raise BitrixError(f"{entity_type} #{entity_id} not found")
        return dict(result)

    def get_owner(self, entity_type: str, entity_id: str) -> str:
        return norm_id(self.get_entity(entity_type, entity_id).get("ASSIGNED_BY_ID"))

    def update_owner_form(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        return self.call_form_full(ENTITY_METHODS[entity_type]["update"], [
            ("id", entity_id),
            ("fields[ASSIGNED_BY_ID]", target_user_id),
            ("params[REGISTER_SONET_EVENT]", "N"),
        ])

    def update_owner_json(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        return self.call_json_full(ENTITY_METHODS[entity_type]["update"], {
            "id": entity_id,
            "fields": {"ASSIGNED_BY_ID": target_user_id},
            "params": {"REGISTER_SONET_EVENT": "N"},
        })

    def get_contact_company_ids(self, contact: Dict[str, Any]) -> Set[str]:
        contact_id = norm_id(contact.get("ID"))
        company_ids: Set[str] = set()
        direct_company_id = norm_id(contact.get("COMPANY_ID"))
        if direct_company_id:
            company_ids.add(direct_company_id)

        for id_key in ("id", "ID"):
            try:
                result = self.call_form("crm.contact.company.items.get", [(id_key, contact_id)])
                if isinstance(result, list):
                    for item in result:
                        cid = norm_id((item or {}).get("COMPANY_ID") or (item or {}).get("ID"))
                        if cid:
                            company_ids.add(cid)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: crm.contact.company.items.get with {id_key}= failed/ignored: {exc}")

        # Lightweight fallback. If this filter is unsupported, it will be ignored safely.
        try:
            params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), ("filter[CONTACT_ID]", contact_id)]
            add_select(params, SELECT_FIELDS["company"])
            for company in self.list_all("crm.company.list", params):
                cid = norm_id(company.get("ID"))
                if cid:
                    company_ids.add(cid)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: crm.company.list by CONTACT_ID failed/ignored: {exc}")
        return company_ids

    def list_deals(self, contact_id: str, company_ids: Set[str], deal_originator_id: str, include_closed_deals: bool) -> Dict[str, Dict[str, Any]]:
        deals: Dict[str, Dict[str, Any]] = {}

        def collect(filter_field: str, filter_value: str, relation: str) -> None:
            params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), (f"filter[{filter_field}]", filter_value)]
            if deal_originator_id:
                params.append(("filter[ORIGINATOR_ID]", deal_originator_id))
            add_select(params, SELECT_FIELDS["deal"])
            for deal in self.list_all("crm.deal.list", params):
                if not include_closed_deals and text(deal.get("CLOSED")).upper() == "Y":
                    continue
                did = norm_id(deal.get("ID"))
                if did:
                    row = dict(deal)
                    row["_relation"] = relation
                    deals[did] = row

        collect("CONTACT_ID", contact_id, "deal_by_contact_id")
        for company_id in sorted(company_ids, key=lambda x: int(x) if x.isdigit() else 0):
            collect("COMPANY_ID", company_id, f"deal_by_company_id:{company_id}")
        return deals


def make_row(entity_type: str, entity: Dict[str, Any], relation: str, target_user_id: str, target_user_name: str, dry_run: bool) -> Dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": norm_id(entity.get("ID")),
        "entity_title": title_of(entity_type, entity),
        "relation": relation,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "from_owner_id_list": norm_id(entity.get("ASSIGNED_BY_ID")),
        "before_owner_id_live": "",
        "form_update_result": "",
        "after_form_owner_id": "",
        "json_update_result": "",
        "after_json_owner_id": "",
        "final_owner_id": "",
        "action_status": "",
        "error": "",
        "company_id": norm_id(entity.get("COMPANY_ID")),
        "contact_id_field": norm_id(entity.get("CONTACT_ID")),
        "originator_id": text(entity.get("ORIGINATOR_ID")),
        "origin_id": text(entity.get("ORIGIN_ID")),
        "stage_id": text(entity.get("STAGE_ID")),
        "closed": text(entity.get("CLOSED")),
    }


def update_row(bx: Bitrix, row: Dict[str, Any], target_user_id: str, dry_run: bool) -> None:
    entity_type = text(row.get("entity_type"))
    entity_id = norm_id(row.get("entity_id"))
    if entity_type not in ENTITY_METHODS or not entity_id:
        row["action_status"] = "invalid_row"
        row["error"] = f"Invalid entity: {entity_type}/{entity_id}"
        return

    before = bx.get_owner(entity_type, entity_id)
    row["before_owner_id_live"] = before
    if before == target_user_id:
        row["final_owner_id"] = before
        row["action_status"] = "already_target"
        return
    if dry_run:
        row["final_owner_id"] = before
        row["action_status"] = "dry_run_no_write"
        return

    form_payload = bx.update_owner_form(entity_type, entity_id, target_user_id)
    row["form_update_result"] = json.dumps(form_payload.get("result"), ensure_ascii=False)[:500]
    after_form = bx.get_owner(entity_type, entity_id)
    row["after_form_owner_id"] = after_form
    if after_form == target_user_id:
        row["final_owner_id"] = after_form
        row["action_status"] = "owner_changed_and_verified"
        return

    json_payload = bx.update_owner_json(entity_type, entity_id, target_user_id)
    row["json_update_result"] = json.dumps(json_payload.get("result"), ensure_ascii=False)[:500]
    after_json = bx.get_owner(entity_type, entity_id)
    row["after_json_owner_id"] = after_json
    row["final_owner_id"] = after_json
    if after_json == target_user_id:
        row["action_status"] = "owner_changed_and_verified_json_fallback"
    else:
        row["action_status"] = "update_accepted_but_owner_not_changed"
        row["error"] = f"Owner did not change: before={before}, after_form={after_form}, after_json={after_json}, target={target_user_id}"


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type", "entity_id", "entity_title", "relation", "target_user_id", "target_user_name", "dry_run",
        "from_owner_id_list", "before_owner_id_live", "form_update_result", "after_form_owner_id",
        "json_update_result", "after_json_owner_id", "final_owner_id", "action_status", "error",
        "company_id", "contact_id_field", "originator_id", "origin_id", "stage_id", "closed",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def summary(rows: List[Dict[str, Any]], contact_id: str, target_user_id: str, target_user_name: str, dry_run: bool) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    errors: List[Dict[str, str]] = []
    for row in rows:
        entity_type = text(row.get("entity_type"))
        status = text(row.get("action_status"))
        by_type[entity_type] = by_type.get(entity_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        if text(row.get("error")):
            errors.append({"entity_type": entity_type, "entity_id": text(row.get("entity_id")), "error": text(row.get("error"))})
    return {
        "contact_id": contact_id,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "total_rows": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "errors": errors[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="FAST reassignment by exact Bitrix contact ID")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--deal-originator-id", default="EQAZYNA", help="Empty = all deals")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--reassign-source-contact", default="true")
    parser.add_argument("--out", default="exports/reassign_by_contact_id_fast_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_by_contact_id_fast_summary.json")
    args = parser.parse_args()

    contact_id = norm_id(args.contact_id)
    target_user_id = norm_id(args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    reassign_source_contact = parse_bool(args.reassign_source_contact, default=True)
    deal_originator_id = text(args.deal_originator_id)

    if not contact_id:
        raise ValueError("contact_id is empty")
    if not target_user_id:
        raise ValueError("target_user_id is empty")

    bx = Bitrix(os.getenv("BITRIX_WEBHOOK_URL", ""), timeout=int(os.getenv("REQUEST_TIMEOUT", "60")))
    target_user_name = bx.validate_user(target_user_id)

    print(f"MODE: {'DRY_RUN' if dry_run else 'WRITE'}")
    print(f"CONTACT_ID: {contact_id}")
    print(f"TARGET_USER: {target_user_id} ({target_user_name})")
    print(f"DEAL_ORIGINATOR_ID: {deal_originator_id or 'ALL'}")
    print("FAST MODE: no related-contact expansion, no delayed per-card verification")

    rows: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()

    def add(entity_type: str, entity: Dict[str, Any], relation: str) -> None:
        entity_id = norm_id(entity.get("ID"))
        if not entity_id:
            return
        key = (entity_type, entity_id)
        if key in seen:
            return
        seen.add(key)
        rows.append(make_row(entity_type, entity, relation, target_user_id, target_user_name, dry_run))

    contact = bx.get_entity("contact", contact_id)
    if reassign_source_contact:
        add("contact", contact, "source_contact")

    company_ids = bx.get_contact_company_ids(contact)
    companies: Dict[str, Dict[str, Any]] = {}
    for company_id in sorted(company_ids, key=lambda x: int(x) if x.isdigit() else 0):
        try:
            company = bx.get_entity("company", company_id)
            companies[company_id] = company
            add("company", company, "company_linked_to_contact")
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "entity_type": "company", "entity_id": company_id, "entity_title": "", "relation": "company_linked_to_contact",
                "target_user_id": target_user_id, "target_user_name": target_user_name, "dry_run": str(dry_run).lower(),
                "action_status": "read_failed", "error": str(exc),
            })

    deals = bx.list_deals(contact_id, set(companies.keys()), deal_originator_id, include_closed_deals)
    for deal in sorted(deals.values(), key=lambda row: int(norm_id(row.get("ID")) or 0)):
        add("deal", deal, text(deal.get("_relation")) or "deal_linked_to_contact_or_company")

    print(f"DISCOVERED: contacts={sum(1 for r in rows if r.get('entity_type') == 'contact')}, companies={sum(1 for r in rows if r.get('entity_type') == 'company')}, deals={sum(1 for r in rows if r.get('entity_type') == 'deal')}, total={len(rows)}")

    had_errors = False
    for index, row in enumerate(rows, start=1):
        entity_type = text(row.get("entity_type"))
        entity_id = norm_id(row.get("entity_id"))
        print(f"[{index}/{len(rows)}] {entity_type} #{entity_id}: {row.get('entity_title')}")
        if text(row.get("action_status")) == "read_failed":
            had_errors = True
            continue
        try:
            update_row(bx, row, target_user_id, dry_run)
            if text(row.get("error")):
                had_errors = True
                print(f"  ERROR: {row.get('error')}")
            else:
                print(f"  {row.get('action_status')} -> owner {row.get('final_owner_id')}")
        except Exception as exc:  # noqa: BLE001
            row["action_status"] = "update_failed"
            row["error"] = str(exc)
            had_errors = True
            print(f"  ERROR: {exc}")

    write_csv(args.out, rows)
    payload = summary(rows, contact_id, target_user_id, target_user_name, dry_run)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
