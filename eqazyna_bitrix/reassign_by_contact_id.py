#!/usr/bin/env python3
"""Reassign a Bitrix CRM package by founder/director contact ID.

Input: a known Bitrix contact ID. The tool moves:
- the contact itself;
- companies linked to that contact;
- deals linked to that contact or linked companies;
- optionally other contacts linked to those companies/deals.

The write path intentionally mirrors the proven Force CRM owner diagnostic:
form-data update first, JSON fallback, then live owner verification.
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
    "company": {"get": "crm.company.get", "update": "crm.company.update"},
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update"},
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update"},
}

SELECT_FIELDS = {
    "company": ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "DATE_MODIFY"],
    "deal": [
        "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "ORIGINATOR_ID", "ORIGIN_ID",
        "CATEGORY_ID", "STAGE_ID", "CLOSED", "DATE_MODIFY",
    ],
    "contact": ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "FULL_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_MODIFY"],
}


class BitrixError(RuntimeError):
    pass


def s(value: Any) -> str:
    return str(value or "").strip()


def norm_id(value: Any) -> str:
    raw = s(value)
    if not raw:
        return ""
    try:
        return str(int(raw))
    except Exception:
        return raw


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = s(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def normalize_webhook(url: str) -> str:
    url = s(url)
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


def title_of(entity_type: str, row: Dict[str, Any]) -> str:
    if entity_type in {"company", "deal"}:
        return s(row.get("TITLE"))
    parts = [s(row.get("LAST_NAME")), s(row.get("NAME")), s(row.get("SECOND_NAME"))]
    full = " ".join(p for p in parts if p).strip()
    return full or s(row.get("FULL_NAME")) or s(row.get("TITLE"))


def add_select(params: List[Tuple[str, Any]], fields: Iterable[str]) -> None:
    for field in fields:
        params.append(("select[]", field))


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = 60, sleep_seconds: float = 0.15) -> None:
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
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:800]}") from exc
        if response.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:2000]}")
        return data

    def call_json_full(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self.session.post(self.base + method + ".json", json=payload or {}, timeout=self.timeout)
        return self._parse_response(method, response)

    def call_json(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self.call_json_full(method, payload).get("result")

    def call_form_full(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Dict[str, Any]:
        response = self.session.post(self.base + method + ".json", data=list(params or []), timeout=self.timeout)
        return self._parse_response(method, response)

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
                next_start = result.get("next", payload.get("next"))
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
        active = s(user.get("ACTIVE")).lower()
        if active in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={user_id} is inactive")
        return " ".join(s(x) for x in [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")] if s(x)).strip() or s(user.get("EMAIL")) or f"ID {user_id}"

    def get_entity(self, entity_type: str, entity_id: str) -> Dict[str, Any]:
        method = ENTITY_METHODS[entity_type]["get"]
        result = self.call_json(method, {"id": entity_id})
        if not isinstance(result, dict) or not result:
            raise BitrixError(f"{entity_type} #{entity_id} not found")
        return dict(result)

    def get_owner(self, entity_type: str, entity_id: str) -> str:
        return norm_id(self.get_entity(entity_type, entity_id).get("ASSIGNED_BY_ID"))

    def update_owner_form(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        method = ENTITY_METHODS[entity_type]["update"]
        return self.call_form_full(method, [
            ("id", entity_id),
            ("fields[ASSIGNED_BY_ID]", target_user_id),
            ("params[REGISTER_SONET_EVENT]", "N"),
        ])

    def update_owner_json(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        method = ENTITY_METHODS[entity_type]["update"]
        return self.call_json_full(method, {
            "id": entity_id,
            "fields": {"ASSIGNED_BY_ID": target_user_id},
            "params": {"REGISTER_SONET_EVENT": "N"},
        })

    def get_contact_company_ids(self, contact_id: str) -> Set[str]:
        company_ids: Set[str] = set()
        contact = self.get_entity("contact", contact_id)
        direct_company_id = norm_id(contact.get("COMPANY_ID"))
        if direct_company_id:
            company_ids.add(direct_company_id)

        # Multiple company binding API. Ignore if the portal/API plan does not expose it.
        try:
            result = self.call_form("crm.contact.company.items.get", [("id", contact_id)])
            if isinstance(result, list):
                for item in result:
                    cid = norm_id((item or {}).get("COMPANY_ID"))
                    if cid:
                        company_ids.add(cid)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: crm.contact.company.items.get failed/ignored: {exc}")

        # Fallback. Some portals allow company filter by CONTACT_ID.
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

    def get_company(self, company_id: str) -> Dict[str, Any]:
        return self.get_entity("company", company_id)

    def list_deals_for_contact_or_companies(
        self,
        contact_id: str,
        company_ids: Set[str],
        *,
        deal_originator_id: str,
        include_closed_deals: bool,
    ) -> Dict[str, Dict[str, Any]]:
        deals: Dict[str, Dict[str, Any]] = {}

        def collect(filter_field: str, filter_value: str) -> None:
            params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), (f"filter[{filter_field}]", filter_value)]
            if deal_originator_id:
                params.append(("filter[ORIGINATOR_ID]", deal_originator_id))
            add_select(params, SELECT_FIELDS["deal"])
            for row in self.list_all("crm.deal.list", params):
                if not include_closed_deals and s(row.get("CLOSED")).upper() == "Y":
                    continue
                did = norm_id(row.get("ID"))
                if did:
                    deals[did] = dict(row)

        collect("CONTACT_ID", contact_id)
        for company_id in sorted(company_ids, key=lambda x: int(x) if x.isdigit() else 0):
            collect("COMPANY_ID", company_id)
        return deals

    def list_contacts_for_company(self, company_id: str) -> Dict[str, Dict[str, Any]]:
        contacts: Dict[str, Dict[str, Any]] = {}
        params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), ("filter[COMPANY_ID]", company_id)]
        add_select(params, SELECT_FIELDS["contact"])
        try:
            for row in self.list_all("crm.contact.list", params):
                cid = norm_id(row.get("ID"))
                if cid:
                    contacts[cid] = dict(row)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: crm.contact.list by COMPANY_ID={company_id} failed/ignored: {exc}")
        return contacts

    def get_deal_contact_ids(self, deal: Dict[str, Any]) -> Set[str]:
        contact_ids: Set[str] = set()
        primary = norm_id(deal.get("CONTACT_ID"))
        if primary:
            contact_ids.add(primary)
        did = norm_id(deal.get("ID"))
        if did:
            try:
                result = self.call_form("crm.deal.contact.items.get", [("id", did)])
                if isinstance(result, list):
                    for item in result:
                        cid = norm_id((item or {}).get("CONTACT_ID"))
                        if cid:
                            contact_ids.add(cid)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: crm.deal.contact.items.get for deal={did} failed/ignored: {exc}")
        return contact_ids


def update_and_verify(
    bx: Bitrix,
    *,
    entity_type: str,
    entity_id: str,
    target_user_id: str,
    dry_run: bool,
    verify_delay: int,
) -> Dict[str, Any]:
    before = bx.get_owner(entity_type, entity_id)
    result: Dict[str, Any] = {
        "from_owner_id_live": before,
        "form_update_result": "",
        "json_update_result": "",
        "verified_owner_id": "",
        "verified_owner_id_after_json": "",
        "verified_owner_id_after_delay": "",
        "action_status": "",
        "error": "",
    }
    if before == target_user_id:
        result["verified_owner_id"] = before
        result["verified_owner_id_after_delay"] = before
        result["action_status"] = "already_target"
        return result
    if dry_run:
        result["action_status"] = "dry_run_no_write"
        return result

    form_payload = bx.update_owner_form(entity_type, entity_id, target_user_id)
    result["form_update_result"] = json.dumps(form_payload.get("result"), ensure_ascii=False)[:500]
    time.sleep(1)
    after_form = bx.get_owner(entity_type, entity_id)
    result["verified_owner_id"] = after_form

    if after_form != target_user_id:
        json_payload = bx.update_owner_json(entity_type, entity_id, target_user_id)
        result["json_update_result"] = json.dumps(json_payload.get("result"), ensure_ascii=False)[:500]
        time.sleep(1)
        after_json = bx.get_owner(entity_type, entity_id)
        result["verified_owner_id_after_json"] = after_json
    else:
        result["verified_owner_id_after_json"] = after_form

    if verify_delay:
        time.sleep(verify_delay)
    after_delay = bx.get_owner(entity_type, entity_id)
    result["verified_owner_id_after_delay"] = after_delay

    if after_delay == target_user_id:
        result["action_status"] = "owner_changed_and_verified"
    elif result["verified_owner_id"] == target_user_id or result["verified_owner_id_after_json"] == target_user_id:
        result["action_status"] = "changed_then_rolled_back"
        result["error"] = f"Owner changed to {target_user_id}, then rolled back to {after_delay}"
    else:
        result["action_status"] = "update_accepted_but_owner_not_changed"
        result["error"] = (
            f"Owner did not change. before={before}, after_form={result['verified_owner_id']}, "
            f"after_json={result['verified_owner_id_after_json']}, after_delay={after_delay}, target={target_user_id}"
        )
    return result


def build_row(
    *,
    entity_type: str,
    entity: Dict[str, Any],
    relation: str,
    contact_id: str,
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
) -> Dict[str, Any]:
    return {
        "contact_id": contact_id,
        "entity_type": entity_type,
        "entity_id": norm_id(entity.get("ID")),
        "entity_title": title_of(entity_type, entity),
        "relation": relation,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "from_owner_id_list": norm_id(entity.get("ASSIGNED_BY_ID")),
        "from_owner_id_live": "",
        "form_update_result": "",
        "json_update_result": "",
        "verified_owner_id": "",
        "verified_owner_id_after_json": "",
        "verified_owner_id_after_delay": "",
        "action_status": "",
        "error": "",
        "company_id": norm_id(entity.get("COMPANY_ID")),
        "contact_id_field": norm_id(entity.get("CONTACT_ID")),
        "originator_id": s(entity.get("ORIGINATOR_ID")),
        "origin_id": s(entity.get("ORIGIN_ID")),
        "stage_id": s(entity.get("STAGE_ID")),
        "closed": s(entity.get("CLOSED")),
    }


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "contact_id", "entity_type", "entity_id", "entity_title", "relation",
        "target_user_id", "target_user_name", "dry_run",
        "from_owner_id_list", "from_owner_id_live",
        "form_update_result", "json_update_result",
        "verified_owner_id", "verified_owner_id_after_json", "verified_owner_id_after_delay",
        "action_status", "error",
        "company_id", "contact_id_field", "originator_id", "origin_id", "stage_id", "closed",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def make_summary(rows: List[Dict[str, Any]], *, contact_id: str, target_user_id: str, target_user_name: str, dry_run: bool) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    errors: List[Dict[str, str]] = []
    for row in rows:
        et = s(row.get("entity_type"))
        st = s(row.get("action_status"))
        by_type[et] = by_type.get(et, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1
        if s(row.get("error")):
            errors.append({"entity_type": et, "entity_id": s(row.get("entity_id")), "error": s(row.get("error"))})
    return {
        "contact_id": contact_id,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "total_rows": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "errors": errors[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reassign Bitrix package by founder/director contact ID")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--deal-originator-id", default="EQAZYNA", help="Use empty string to include all deals")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--include-related-contacts", default="true")
    parser.add_argument("--verify-delay-seconds", default="5")
    parser.add_argument("--out", default="exports/reassign_by_contact_id_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_by_contact_id_summary.json")
    args = parser.parse_args()

    contact_id = norm_id(args.contact_id)
    target_user_id = norm_id(args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    deal_originator_id = s(args.deal_originator_id)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    include_related_contacts = parse_bool(args.include_related_contacts, default=True)
    verify_delay = max(0, int(s(args.verify_delay_seconds) or "5"))

    if not contact_id:
        raise ValueError("contact_id is empty")
    if not target_user_id:
        raise ValueError("target_user_id is empty")

    bx = Bitrix(os.getenv("BITRIX_WEBHOOK_URL", ""), timeout=int(os.getenv("REQUEST_TIMEOUT", "60")))
    target_user_name = bx.validate_user(target_user_id)

    print(f"MODE: {'DRY_RUN' if dry_run else 'WRITE'}")
    print(f"CONTACT ID: {contact_id}")
    print(f"TARGET: {target_user_id} ({target_user_name})")
    print(f"DEAL ORIGINATOR FILTER: {deal_originator_id or 'ALL'}")

    rows: List[Dict[str, Any]] = []
    seen_entities: Set[Tuple[str, str]] = set()

    def add_entity(entity_type: str, entity: Dict[str, Any], relation: str) -> None:
        entity_id = norm_id(entity.get("ID"))
        if not entity_id:
            return
        key = (entity_type, entity_id)
        if key in seen_entities:
            return
        seen_entities.add(key)
        rows.append(build_row(
            entity_type=entity_type,
            entity=entity,
            relation=relation,
            contact_id=contact_id,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            dry_run=dry_run,
        ))

    contact = bx.get_entity("contact", contact_id)
    add_entity("contact", contact, "source_contact")

    company_ids = bx.get_contact_company_ids(contact_id)
    companies: Dict[str, Dict[str, Any]] = {}
    for company_id in sorted(company_ids, key=lambda x: int(x) if x.isdigit() else 0):
        try:
            company = bx.get_company(company_id)
            companies[company_id] = company
            add_entity("company", company, "company_linked_to_contact")
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "contact_id": contact_id,
                "entity_type": "company",
                "entity_id": company_id,
                "entity_title": "",
                "relation": "company_linked_to_contact",
                "target_user_id": target_user_id,
                "target_user_name": target_user_name,
                "dry_run": str(dry_run).lower(),
                "action_status": "read_failed",
                "error": str(exc),
            })

    deals = bx.list_deals_for_contact_or_companies(
        contact_id,
        set(companies.keys()),
        deal_originator_id=deal_originator_id,
        include_closed_deals=include_closed_deals,
    )
    for deal in sorted(deals.values(), key=lambda x: int(norm_id(x.get("ID")) or 0)):
        add_entity("deal", deal, "deal_linked_to_contact_or_company")

    if include_related_contacts:
        related_contact_ids: Set[str] = {contact_id}
        for company_id in companies:
            for cid, row in bx.list_contacts_for_company(company_id).items():
                related_contact_ids.add(cid)
                add_entity("contact", row, "contact_linked_to_company")
        for deal in deals.values():
            for cid in bx.get_deal_contact_ids(deal):
                related_contact_ids.add(cid)
        for cid in sorted(related_contact_ids, key=lambda x: int(x) if x.isdigit() else 0):
            if ("contact", cid) in seen_entities:
                continue
            try:
                add_entity("contact", bx.get_entity("contact", cid), "contact_linked_to_deal")
            except Exception as exc:  # noqa: BLE001
                rows.append({
                    "contact_id": contact_id,
                    "entity_type": "contact",
                    "entity_id": cid,
                    "entity_title": "",
                    "relation": "contact_linked_to_deal",
                    "target_user_id": target_user_id,
                    "target_user_name": target_user_name,
                    "dry_run": str(dry_run).lower(),
                    "action_status": "read_failed",
                    "error": str(exc),
                })

    # Execute updates after the full list is built. This keeps dry-run and write lists identical.
    had_errors = False
    for row in rows:
        entity_type = s(row.get("entity_type"))
        entity_id = norm_id(row.get("entity_id"))
        if row.get("action_status") == "read_failed":
            had_errors = True
            continue
        if entity_type not in ENTITY_METHODS or not entity_id:
            row["action_status"] = "invalid_row"
            row["error"] = f"Invalid entity_type/entity_id: {entity_type}/{entity_id}"
            had_errors = True
            continue
        try:
            result = update_and_verify(
                bx,
                entity_type=entity_type,
                entity_id=entity_id,
                target_user_id=target_user_id,
                dry_run=dry_run,
                verify_delay=verify_delay,
            )
            row.update(result)
            if s(row.get("error")):
                had_errors = True
        except Exception as exc:  # noqa: BLE001
            row["action_status"] = "update_failed"
            row["error"] = str(exc)
            had_errors = True

    write_csv(args.out, rows)
    summary = make_summary(rows, contact_id=contact_id, target_user_id=target_user_id, target_user_name=target_user_name, dry_run=dry_run)
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
