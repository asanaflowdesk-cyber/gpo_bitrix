#!/usr/bin/env python3
"""Ultra-fast Bitrix CRM owner reassignment by exact contact ID.

Fast path only:
- exact source contact;
- companies directly bound to this contact;
- deals directly bound to the contact;
- deals bound to the found companies.

Writes are made through Bitrix REST batch, not one object at a time.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import requests

TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off", ""}

ENTITY_METHODS = {
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update", "list": "crm.contact.list"},
    "company": {"get": "crm.company.get", "update": "crm.company.update", "list": "crm.company.list"},
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update", "list": "crm.deal.list"},
}

SELECT_FIELDS = {
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
        return text(row.get("TITLE")) or f"{entity_type} #{row.get('ID')}"
    parts = [text(row.get("LAST_NAME")), text(row.get("NAME")), text(row.get("SECOND_NAME"))]
    return " ".join(part for part in parts if part).strip() or text(row.get("FULL_NAME")) or f"Contact #{row.get('ID')}"


def add_select(params: List[Tuple[str, Any]], fields: Iterable[str]) -> None:
    for field in fields:
        params.append(("select[]", field))


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for pos in range(0, len(items), size):
        yield items[pos:pos + size]


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = 60) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.session = requests.Session()

    def _parse_response(self, method: str, response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:1000]}") from exc
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

    def batch(self, commands: Dict[str, str], halt: bool = False) -> Dict[str, Any]:
        if not commands:
            return {"result": {}, "result_error": {}}
        params: List[Tuple[str, Any]] = [("halt", "1" if halt else "0")]
        for key, command in commands.items():
            params.append((f"cmd[{key}]", command))
        payload = self.call_form_full("batch", params).get("result") or {}
        if not isinstance(payload, dict):
            raise BitrixError(f"batch: unexpected response: {payload!r}")
        return payload

    def validate_user(self, user_id: str) -> str:
        result = self.call_json("user.get", {"ID": user_id})
        user = dict(result[0]) if isinstance(result, list) and result else {}
        if not user:
            raise ValueError(f"target_user_id={user_id} not found by user.get")
        active = text(user.get("ACTIVE")).lower()
        if active in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={user_id} is inactive")
        return self._user_label(user_id, user)

    @staticmethod
    def _user_label(user_id: str, user: Dict[str, Any]) -> str:
        name = " ".join(
            text(value)
            for value in [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")]
            if text(value)
        ).strip()
        return name or text(user.get("EMAIL")) or f"ID {user_id}"

    def get_user_labels(self, user_ids: Iterable[str]) -> Dict[str, str]:
        labels: Dict[str, str] = {}
        for user_id in sorted({norm_id(value) for value in user_ids if norm_id(value)}, key=lambda value: int(value)):
            try:
                result = self.call_json("user.get", {"ID": user_id})
                user = dict(result[0]) if isinstance(result, list) and result else {}
                labels[user_id] = self._user_label(user_id, user) if user else f"ID {user_id}"
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: user.get failed for user #{user_id}: {exc}")
                labels[user_id] = f"ID {user_id}"
        return labels

    def add_contact_timeline_comment(self, contact_id: str, comment: str) -> str:
        result = self.call_json(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": int(contact_id),
                    "ENTITY_TYPE": "contact",
                    "COMMENT": comment,
                }
            },
        )
        if result in (None, "", False):
            raise BitrixError(f"crm.timeline.comment.add returned empty result for contact #{contact_id}")
        return str(result)

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        result = self.call_json("crm.contact.get", {"id": contact_id})
        if not isinstance(result, dict) or not result:
            raise BitrixError(f"contact #{contact_id} not found")
        return dict(result)

    def get_contact_company_ids(self, contact: Dict[str, Any]) -> Set[str]:
        contact_id = norm_id(contact.get("ID"))
        company_ids: Set[str] = set()
        direct_company_id = norm_id(contact.get("COMPANY_ID"))
        if direct_company_id:
            company_ids.add(direct_company_id)

        # Direct binding method. It is authoritative for secondary company
        # links, so failures must stop the workflow instead of producing a
        # silently incomplete package.
        result = self.call_form("crm.contact.company.items.get", [("id", contact_id)])
        if isinstance(result, list):
            for item in result:
                cid = norm_id((item or {}).get("COMPANY_ID") or (item or {}).get("ID"))
                if cid:
                    company_ids.add(cid)
        return company_ids

    def batch_get_entities(self, entity_type: str, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        method = ENTITY_METHODS[entity_type]["get"]
        clean_ids = [norm_id(x) for x in ids if norm_id(x)]
        for part in chunked(clean_ids, 50):
            commands = {f"g{idx}": method + "?" + urlencode({"id": entity_id}) for idx, entity_id in enumerate(part, start=1)}
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            if batch_errors:
                details = "; ".join(
                    f"{entity_type} #{entity_id}: {batch_errors.get(f'g{idx}')}"
                    for idx, entity_id in enumerate(part, start=1)
                    if f"g{idx}" in batch_errors
                )
                raise BitrixError(f"Failed to read linked {entity_type} records: {details}")
            for idx, entity_id in enumerate(part, start=1):
                key = f"g{idx}"
                row = batch_result.get(key)
                if isinstance(row, dict) and row:
                    result[entity_id] = dict(row)
        return result

    def list_all(self, method: str, params_base: List[Tuple[str, Any]], max_pages: int = 20) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start: Any = 0
        pages = 0
        while True:
            pages += 1
            if pages > max_pages:
                raise BitrixError(f"{method}: stopped after max_pages={max_pages}; narrow the filter")
            params = list(params_base)
            params.append(("start", start))
            payload = self.call_form_full(method, params)
            result = payload.get("result")
            items = result if isinstance(result, list) else []
            for item in items:
                if isinstance(item, dict):
                    rows.append(dict(item))
            next_start = payload.get("next")
            if next_start in (None, "", False):
                break
            start = next_start
        return rows

    def list_deals(self, contact_id: str, company_ids: Set[str], deal_originator_id: str, include_closed_deals: bool, max_pages: int) -> Dict[str, Dict[str, Any]]:
        deals: Dict[str, Dict[str, Any]] = {}

        def collect(filter_field: str, filter_value: str, relation: str) -> None:
            params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), (f"filter[{filter_field}]", filter_value)]
            if deal_originator_id:
                params.append(("filter[ORIGINATOR_ID]", deal_originator_id))
            add_select(params, SELECT_FIELDS["deal"])
            for deal in self.list_all("crm.deal.list", params, max_pages=max_pages):
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

    def batch_update_owners(self, rows: List[Dict[str, Any]], target_user_id: str, verify: bool) -> None:
        to_update = [row for row in rows if norm_id(row.get("before_owner_id")) != target_user_id]
        for row in rows:
            if norm_id(row.get("before_owner_id")) == target_user_id:
                row["action_status"] = "already_target"
                row["final_owner_id"] = target_user_id

        # Bitrix batch accepts max 50 commands. Split safely.
        for part in chunked(to_update, 50):
            commands: Dict[str, str] = {}
            key_to_row: Dict[str, Dict[str, Any]] = {}
            for idx, row in enumerate(part, start=1):
                entity_type = text(row.get("entity_type"))
                entity_id = norm_id(row.get("entity_id"))
                method = ENTITY_METHODS[entity_type]["update"]
                params = [
                    ("id", entity_id),
                    ("fields[ASSIGNED_BY_ID]", target_user_id),
                    ("params[REGISTER_SONET_EVENT]", "N"),
                ]
                key = f"u{idx}"
                commands[key] = method + "?" + urlencode(params)
                key_to_row[key] = row

            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, row in key_to_row.items():
                if key in batch_errors:
                    row["action_status"] = "update_failed"
                    row["error"] = json.dumps(batch_errors.get(key), ensure_ascii=False)[:1000]
                else:
                    row["update_result"] = json.dumps(batch_result.get(key), ensure_ascii=False)[:500]
                    row["action_status"] = "update_sent"

        if not verify:
            for row in rows:
                if text(row.get("action_status")) == "update_sent":
                    row["action_status"] = "update_sent_not_verified"
                    row["final_owner_id"] = target_user_id
            return

        # One quick batch verification, no waiting.
        candidates = [row for row in rows if text(row.get("action_status")) in {"update_sent", "already_target"}]
        for part in chunked(candidates, 50):
            commands = {}
            key_to_row = {}
            for idx, row in enumerate(part, start=1):
                entity_type = text(row.get("entity_type"))
                entity_id = norm_id(row.get("entity_id"))
                method = ENTITY_METHODS[entity_type]["get"]
                key = f"v{idx}"
                commands[key] = method + "?" + urlencode({"id": entity_id})
                key_to_row[key] = row

            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, row in key_to_row.items():
                if key in batch_errors:
                    row["action_status"] = "verify_failed"
                    row["error"] = json.dumps(batch_errors.get(key), ensure_ascii=False)[:1000]
                    continue
                entity = batch_result.get(key) or {}
                owner = norm_id(entity.get("ASSIGNED_BY_ID"))
                row["verified_owner_id"] = owner
                row["final_owner_id"] = owner
                if owner == target_user_id:
                    if text(row.get("action_status")) == "already_target":
                        row["action_status"] = "already_target_verified"
                    else:
                        row["action_status"] = "owner_changed_and_verified"
                else:
                    row["action_status"] = "update_sent_but_owner_not_changed"
                    row["error"] = f"verified_owner_id={owner}; target_user_id={target_user_id}"


def make_row(entity_type: str, entity: Dict[str, Any], relation: str, target_user_id: str, target_user_name: str, dry_run: bool) -> Dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": norm_id(entity.get("ID")),
        "entity_title": title_of(entity_type, entity),
        "relation": relation,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "before_owner_id": norm_id(entity.get("ASSIGNED_BY_ID")),
        "update_result": "",
        "verified_owner_id": "",
        "final_owner_id": "",
        "action_status": "planned" if dry_run else "pending_update",
        "error": "",
        "company_id": norm_id(entity.get("COMPANY_ID")),
        "contact_id_field": norm_id(entity.get("CONTACT_ID")),
        "originator_id": text(entity.get("ORIGINATOR_ID")),
        "origin_id": text(entity.get("ORIGIN_ID")),
        "stage_id": text(entity.get("STAGE_ID")),
        "closed": text(entity.get("CLOSED")),
    }


def build_reassignment_comment(
    rows: List[Dict[str, Any]],
    contact_id: str,
    source_contact_owner_id: str,
    target_user_id: str,
    target_user_name: str,
    user_labels: Dict[str, str],
) -> str:
    moved_statuses = {"owner_changed_and_verified", "update_sent_not_verified"}
    moved_rows = [row for row in rows if text(row.get("action_status")) in moved_statuses]

    counts = {"company": 0, "deal": 0, "contact": 0}
    for row in moved_rows:
        entity_type = text(row.get("entity_type"))
        if entity_type in counts:
            counts[entity_type] += 1

    previous_owner_ids = sorted(
        {norm_id(row.get("before_owner_id")) for row in moved_rows if norm_id(row.get("before_owner_id"))},
        key=lambda value: int(value),
    )

    def label(user_id: str) -> str:
        if not user_id:
            return "не указан"
        return f"{user_labels.get(user_id, f'ID {user_id}')} (ID {user_id})"

    previous_owners = ", ".join(label(user_id) for user_id in previous_owner_ids) or "не указаны"
    source_owner = label(source_contact_owner_id)

    return (
        "Служебная отметка о перераспределении.\n\n"
        "Пакет учредителя переназначен вручную с другого ответственного.\n"
        f"Ответственный карточки учредителя до запуска: {source_owner}.\n"
        f"Предыдущие ответственные связанных элементов: {previous_owners}.\n"
        f"Новый ответственный: {target_user_name} (ID {target_user_id}).\n\n"
        f"Перенесено: компаний — {counts['company']}, сделок — {counts['deal']}, контактов — {counts['contact']}.\n"
        f"Основание: административное перераспределение по контакту #{contact_id}."
    )


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type", "entity_id", "entity_title", "relation", "target_user_id", "target_user_name", "dry_run",
        "before_owner_id", "update_result", "verified_owner_id", "final_owner_id", "action_status", "error",
        "company_id", "contact_id_field", "originator_id", "origin_id", "stage_id", "closed",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def make_summary(
    rows: List[Dict[str, Any]],
    contact_id: str,
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    timeline_comment: Dict[str, Any],
) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for row in rows:
        by_type[text(row.get("entity_type"))] = by_type.get(text(row.get("entity_type")), 0) + 1
        by_status[text(row.get("action_status"))] = by_status.get(text(row.get("action_status")), 0) + 1
    errors = [
        {"entity_type": text(r.get("entity_type")), "entity_id": text(r.get("entity_id")), "error": text(r.get("error"))}
        for r in rows if text(r.get("error"))
    ]
    return {
        "contact_id": contact_id,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "total_rows": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "timeline_comment": timeline_comment,
        "errors": errors[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BATCH reassignment by exact Bitrix contact ID")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--deal-originator-id", default="EQAZYNA", help="Empty = all deals")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--reassign-source-contact", default="true")
    parser.add_argument("--verify", default="true", help="Quick batch verification after write. No delay.")
    parser.add_argument("--add-timeline-comment", default="true")
    parser.add_argument("--max-list-pages", default="20")
    parser.add_argument("--out", default="exports/reassign_by_contact_id_batch_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_by_contact_id_batch_summary.json")
    args = parser.parse_args()

    contact_id = norm_id(args.contact_id)
    target_user_id = norm_id(args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    reassign_source_contact = parse_bool(args.reassign_source_contact, default=True)
    verify = parse_bool(args.verify, default=True)
    add_timeline_comment = parse_bool(args.add_timeline_comment, default=True)
    deal_originator_id = text(args.deal_originator_id)
    max_list_pages = int(args.max_list_pages)

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
    print("BATCH MODE: no related-contact expansion, no per-object writes, no delayed verification")

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

    contact = bx.get_contact(contact_id)
    if reassign_source_contact:
        add("contact", contact, "source_contact")

    company_ids = bx.get_contact_company_ids(contact)
    companies = bx.batch_get_entities("company", sorted(company_ids, key=lambda x: int(x) if x.isdigit() else 0))
    for company_id in sorted(companies, key=lambda x: int(x) if x.isdigit() else 0):
        add("company", companies[company_id], "company_linked_to_contact")

    deals = bx.list_deals(contact_id, set(companies.keys()), deal_originator_id, include_closed_deals, max_pages=max_list_pages)
    for deal in sorted(deals.values(), key=lambda row: int(norm_id(row.get("ID")) or 0)):
        add("deal", deal, text(deal.get("_relation")) or "deal_linked_to_contact_or_company")

    print(f"DISCOVERED: contacts={sum(1 for r in rows if r.get('entity_type') == 'contact')}, companies={sum(1 for r in rows if r.get('entity_type') == 'company')}, deals={sum(1 for r in rows if r.get('entity_type') == 'deal')}, total={len(rows)}")

    timeline_comment: Dict[str, Any] = {
        "contact_id": contact_id,
        "enabled": add_timeline_comment,
        "status": "dry_run_not_added" if dry_run else "not_attempted",
        "comment_id": "",
        "error": "",
    }

    if dry_run:
        for row in rows:
            row["action_status"] = "dry_run_planned"
            row["final_owner_id"] = row.get("before_owner_id", "")
    else:
        bx.batch_update_owners(rows, target_user_id, verify=verify)
        moved_statuses = {"owner_changed_and_verified", "update_sent_not_verified"}
        moved_rows = [row for row in rows if text(row.get("action_status")) in moved_statuses]
        if not add_timeline_comment:
            timeline_comment["status"] = "disabled"
        elif not moved_rows:
            timeline_comment["status"] = "skipped_no_owner_changes"
        else:
            previous_owner_ids = {
                norm_id(row.get("before_owner_id"))
                for row in moved_rows
                if norm_id(row.get("before_owner_id"))
            }
            source_contact_owner_id = norm_id(contact.get("ASSIGNED_BY_ID"))
            if source_contact_owner_id:
                previous_owner_ids.add(source_contact_owner_id)
            user_labels = bx.get_user_labels(previous_owner_ids)
            comment_text = build_reassignment_comment(
                rows=rows,
                contact_id=contact_id,
                source_contact_owner_id=source_contact_owner_id,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                user_labels=user_labels,
            )
            try:
                timeline_comment["comment_id"] = bx.add_contact_timeline_comment(contact_id, comment_text)
                timeline_comment["status"] = "added"
            except Exception as exc:  # noqa: BLE001
                timeline_comment["status"] = "failed"
                timeline_comment["error"] = str(exc)[:2000]

    write_csv(args.out, rows)
    payload = make_summary(
        rows,
        contact_id,
        target_user_id,
        target_user_name,
        dry_run,
        timeline_comment,
    )
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("errors") or timeline_comment.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
