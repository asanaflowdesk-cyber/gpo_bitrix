#!/usr/bin/env python3
"""Fast Bitrix CRM owner reassignment by exact company ID.

The workflow moves the company and its directly related CRM entities:
- the source company;
- deals whose COMPANY_ID equals the source company;
- contacts linked to the company;
- optionally, contacts linked to those deals.

Specific company, deal and contact IDs can be excluded explicitly. Updates and
verification are executed through Bitrix REST batch calls (up to 50 commands per
request). No manager configuration is used: the target only has to be an active
Bitrix user.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import requests

TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off", ""}

ENTITY_METHODS = {
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update"},
    "company": {"get": "crm.company.get", "update": "crm.company.update"},
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update"},
}

COMPANY_SELECT = ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID"]
CONTACT_SELECT = [
    "ID",
    "LAST_NAME",
    "NAME",
    "SECOND_NAME",
    "ASSIGNED_BY_ID",
    "COMPANY_ID",
    "ORIGINATOR_ID",
    "ORIGIN_ID",
]
DEAL_SELECT = [
    "ID",
    "TITLE",
    "ASSIGNED_BY_ID",
    "COMPANY_ID",
    "CONTACT_ID",
    "ORIGINATOR_ID",
    "ORIGIN_ID",
    "CATEGORY_ID",
    "STAGE_ID",
    "CLOSED",
]


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
    except (TypeError, ValueError):
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


def parse_id_set(raw: Any) -> Set[str]:
    """Parse comma/semicolon/space/newline separated numeric Bitrix IDs."""
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = re.split(r"[\s,;]+", text(raw))
    result: Set[str] = set()
    for value in values:
        normalized = norm_id(value)
        if not normalized:
            continue
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ValueError(f"Invalid Bitrix ID in exclusion list: {value!r}")
        result.add(normalized)
    return result


def normalize_webhook(url: str) -> str:
    url = text(url)
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


def add_select(params: List[Tuple[str, Any]], fields: Iterable[str]) -> None:
    for field in fields:
        params.append(("select[]", field))


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for pos in range(0, len(items), size):
        yield items[pos : pos + size]


def entity_title(entity_type: str, entity: Dict[str, Any]) -> str:
    if entity_type in {"company", "deal"}:
        return text(entity.get("TITLE")) or f"{entity_type} #{entity.get('ID')}"
    parts = [text(entity.get("LAST_NAME")), text(entity.get("NAME")), text(entity.get("SECOND_NAME"))]
    return " ".join(part for part in parts if part) or f"contact #{entity.get('ID')}"


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

    def get_company(self, company_id: str) -> Dict[str, Any]:
        result = self.call_json("crm.company.get", {"id": company_id})
        if not isinstance(result, dict) or not result:
            raise BitrixError(f"company #{company_id} not found")
        return dict(result)

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        result = self.call_json("crm.contact.get", {"id": contact_id})
        if not isinstance(result, dict) or not result:
            raise BitrixError(f"contact #{contact_id} not found")
        return dict(result)

    def list_all(self, method: str, params_base: List[Tuple[str, Any]], max_pages: int = 100) -> List[Dict[str, Any]]:
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
            if isinstance(result, list):
                rows.extend(dict(item) for item in result if isinstance(item, dict))
            next_start = payload.get("next")
            if next_start in (None, "", False):
                break
            start = next_start
        return rows

    def list_company_deals(
        self,
        company_id: str,
        include_closed_deals: bool,
        deal_originator_id: str,
        max_pages: int,
    ) -> Dict[str, Dict[str, Any]]:
        params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), ("filter[COMPANY_ID]", company_id)]
        if deal_originator_id:
            params.append(("filter[ORIGINATOR_ID]", deal_originator_id))
        add_select(params, DEAL_SELECT)
        result: Dict[str, Dict[str, Any]] = {}
        for deal in self.list_all("crm.deal.list", params, max_pages=max_pages):
            if not include_closed_deals and text(deal.get("CLOSED")).upper() == "Y":
                continue
            deal_id = norm_id(deal.get("ID"))
            if deal_id:
                result[deal_id] = deal
        return result

    def list_direct_company_contact_ids(self, company_id: str, max_pages: int) -> Set[str]:
        contact_ids: Set[str] = set()

        # Multi-company bindings.
        try:
            result = self.call_form("crm.company.contact.items.get", [("id", company_id)])
            if isinstance(result, list):
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    contact_id = norm_id(item.get("CONTACT_ID") or item.get("ID"))
                    if contact_id:
                        contact_ids.add(contact_id)
        except BitrixError as exc:
            print(f"WARN: crm.company.contact.items.get failed: {exc}")

        # Primary COMPANY_ID binding. This catches portals where relation methods
        # return only secondary bindings or are restricted for the webhook user.
        params: List[Tuple[str, Any]] = [("order[ID]", "ASC"), ("filter[COMPANY_ID]", company_id)]
        add_select(params, ["ID"])
        for contact in self.list_all("crm.contact.list", params, max_pages=max_pages):
            contact_id = norm_id(contact.get("ID"))
            if contact_id:
                contact_ids.add(contact_id)
        return contact_ids

    def list_deal_contact_ids(self, deal_ids: Sequence[str]) -> Set[str]:
        contact_ids: Set[str] = set()
        clean_ids = [norm_id(value) for value in deal_ids if norm_id(value)]
        for part in chunked(clean_ids, 50):
            commands = {
                f"d{index}": "crm.deal.contact.items.get?" + urlencode({"id": deal_id})
                for index, deal_id in enumerate(part, start=1)
            }
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for index, deal_id in enumerate(part, start=1):
                key = f"d{index}"
                if key in batch_errors:
                    print(f"WARN: deal #{deal_id} contacts failed: {batch_errors[key]}")
                    continue
                items = batch_result.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    contact_id = norm_id(item.get("CONTACT_ID") or item.get("ID"))
                    if contact_id:
                        contact_ids.add(contact_id)
        return contact_ids

    def batch_get_entities(self, entity_type: str, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        method = ENTITY_METHODS[entity_type]["get"]
        clean_ids = sorted({norm_id(value) for value in ids if norm_id(value)}, key=lambda value: int(value))
        for part in chunked(clean_ids, 50):
            commands = {
                f"g{index}": method + "?" + urlencode({"id": entity_id})
                for index, entity_id in enumerate(part, start=1)
            }
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for index, entity_id in enumerate(part, start=1):
                key = f"g{index}"
                if key in batch_errors:
                    print(f"WARN: {entity_type} #{entity_id} get failed: {batch_errors[key]}")
                    continue
                entity = batch_result.get(key)
                if isinstance(entity, dict) and entity:
                    result[entity_id] = dict(entity)
        return result

    def batch_update_owners(self, rows: List[Dict[str, Any]], target_user_id: str, verify: bool) -> None:
        pending = [row for row in rows if text(row.get("action_status")) == "pending_update"]
        for row in pending:
            if norm_id(row.get("before_owner_id")) == target_user_id:
                row["action_status"] = "already_target"
                row["final_owner_id"] = target_user_id

        to_update = [row for row in pending if text(row.get("action_status")) == "pending_update"]
        for part in chunked(to_update, 50):
            commands: Dict[str, str] = {}
            key_to_row: Dict[str, Dict[str, Any]] = {}
            for index, row in enumerate(part, start=1):
                entity_type = text(row.get("entity_type"))
                entity_id = norm_id(row.get("entity_id"))
                params = [
                    ("id", entity_id),
                    ("fields[ASSIGNED_BY_ID]", target_user_id),
                    ("params[REGISTER_SONET_EVENT]", "N"),
                ]
                key = f"u{index}"
                commands[key] = ENTITY_METHODS[entity_type]["update"] + "?" + urlencode(params)
                key_to_row[key] = row

            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, row in key_to_row.items():
                if key in batch_errors:
                    row["action_status"] = "update_failed"
                    row["error"] = json.dumps(batch_errors[key], ensure_ascii=False)[:1000]
                else:
                    row["update_result"] = json.dumps(batch_result.get(key), ensure_ascii=False)[:500]
                    row["action_status"] = "update_sent"

        if not verify:
            for row in rows:
                if text(row.get("action_status")) == "update_sent":
                    row["action_status"] = "update_sent_not_verified"
                    row["final_owner_id"] = target_user_id
            return

        candidates = [
            row
            for row in rows
            if text(row.get("action_status")) in {"update_sent", "already_target"}
        ]
        for part in chunked(candidates, 50):
            commands: Dict[str, str] = {}
            key_to_row: Dict[str, Dict[str, Any]] = {}
            for index, row in enumerate(part, start=1):
                entity_type = text(row.get("entity_type"))
                entity_id = norm_id(row.get("entity_id"))
                key = f"v{index}"
                commands[key] = ENTITY_METHODS[entity_type]["get"] + "?" + urlencode({"id": entity_id})
                key_to_row[key] = row

            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, row in key_to_row.items():
                if key in batch_errors:
                    row["action_status"] = "verify_failed"
                    row["error"] = json.dumps(batch_errors[key], ensure_ascii=False)[:1000]
                    continue
                entity = batch_result.get(key) or {}
                owner = norm_id(entity.get("ASSIGNED_BY_ID"))
                row["verified_owner_id"] = owner
                row["final_owner_id"] = owner
                if owner == target_user_id:
                    row["action_status"] = (
                        "already_target_verified"
                        if text(row.get("action_status")) == "already_target"
                        else "owner_changed_and_verified"
                    )
                else:
                    row["action_status"] = "update_sent_but_owner_not_changed"
                    row["error"] = f"verified_owner_id={owner}; target_user_id={target_user_id}"


def make_row(
    entity_type: str,
    entity: Dict[str, Any],
    relation: str,
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    excluded: bool,
) -> Dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": norm_id(entity.get("ID")),
        "entity_title": entity_title(entity_type, entity),
        "relation": relation,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "before_owner_id": norm_id(entity.get("ASSIGNED_BY_ID")),
        "update_result": "",
        "verified_owner_id": "",
        "final_owner_id": norm_id(entity.get("ASSIGNED_BY_ID")) if excluded or dry_run else "",
        "action_status": "excluded" if excluded else ("dry_run_planned" if dry_run else "pending_update"),
        "error": "",
        "company_id": norm_id(entity.get("COMPANY_ID")),
        "contact_id_field": norm_id(entity.get("CONTACT_ID")),
        "originator_id": text(entity.get("ORIGINATOR_ID")),
        "origin_id": text(entity.get("ORIGIN_ID")),
        "category_id": text(entity.get("CATEGORY_ID")),
        "stage_id": text(entity.get("STAGE_ID")),
        "closed": text(entity.get("CLOSED")),
    }


def build_rows(
    company: Dict[str, Any],
    deals: Dict[str, Dict[str, Any]],
    contacts: Dict[str, Dict[str, Any]],
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    excluded_company_ids: Set[str],
    excluded_deal_ids: Set[str],
    excluded_contact_ids: Set[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    company_id = norm_id(company.get("ID"))
    rows.append(
        make_row(
            "company",
            company,
            "source_company",
            target_user_id,
            target_user_name,
            dry_run,
            company_id in excluded_company_ids,
        )
    )
    for deal_id in sorted(deals, key=lambda value: int(value)):
        rows.append(
            make_row(
                "deal",
                deals[deal_id],
                "deal_by_company_id",
                target_user_id,
                target_user_name,
                dry_run,
                deal_id in excluded_deal_ids,
            )
        )
    for contact_id in sorted(contacts, key=lambda value: int(value)):
        rows.append(
            make_row(
                "contact",
                contacts[contact_id],
                "contact_linked_to_company_or_company_deal",
                target_user_id,
                target_user_name,
                dry_run,
                contact_id in excluded_contact_ids,
            )
        )
    return rows


def build_reassignment_comment(
    rows: List[Dict[str, Any]],
    company_id: str,
    founder_contact_id: str,
    founder_contact_owner_id: str,
    target_user_id: str,
    target_user_name: str,
    user_labels: Dict[str, str],
) -> str:
    moved_statuses = {"owner_changed_and_verified", "update_sent_not_verified"}
    moved_rows = [row for row in rows if text(row.get("action_status")) in moved_statuses]

    moved_counts = {"company": 0, "deal": 0, "contact": 0}
    excluded_counts = {"company": 0, "deal": 0, "contact": 0}
    for row in rows:
        entity_type = text(row.get("entity_type"))
        if entity_type not in moved_counts:
            continue
        status = text(row.get("action_status"))
        if status in moved_statuses:
            moved_counts[entity_type] += 1
        elif status == "excluded":
            excluded_counts[entity_type] += 1

    previous_owner_ids = sorted(
        {norm_id(row.get("before_owner_id")) for row in moved_rows if norm_id(row.get("before_owner_id"))},
        key=lambda value: int(value),
    )

    def label(user_id: str) -> str:
        if not user_id:
            return "не указан"
        return f"{user_labels.get(user_id, f'ID {user_id}')} (ID {user_id})"

    previous_owners = ", ".join(label(user_id) for user_id in previous_owner_ids) or "не указаны"
    founder_owner = label(founder_contact_owner_id)

    lines = [
        "Служебная отметка о перераспределении.",
        "",
        "Пакет компании переназначен вручную с другого ответственного.",
        f"Ответственный карточки учредителя до запуска: {founder_owner}.",
        f"Предыдущие ответственные связанных элементов: {previous_owners}.",
        f"Новый ответственный: {target_user_name} (ID {target_user_id}).",
        "",
        (
            f"Перенесено: компаний — {moved_counts['company']}, "
            f"сделок — {moved_counts['deal']}, контактов — {moved_counts['contact']}."
        ),
    ]
    if any(excluded_counts.values()):
        lines.append(
            f"Исключено из переноса: компаний — {excluded_counts['company']}, "
            f"сделок — {excluded_counts['deal']}, контактов — {excluded_counts['contact']}."
        )
    lines.append(
        f"Основание: административное перераспределение по компании #{company_id}; "
        f"карточка учредителя #{founder_contact_id}."
    )
    return "\n".join(lines)


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type",
        "entity_id",
        "entity_title",
        "relation",
        "target_user_id",
        "target_user_name",
        "dry_run",
        "before_owner_id",
        "update_result",
        "verified_owner_id",
        "final_owner_id",
        "action_status",
        "error",
        "company_id",
        "contact_id_field",
        "originator_id",
        "origin_id",
        "category_id",
        "stage_id",
        "closed",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def make_summary(
    rows: List[Dict[str, Any]],
    company_id: str,
    founder_contact_id: str,
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    requested_exclusions: Dict[str, Set[str]],
    timeline_comment: Dict[str, Any],
) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    discovered_ids: Dict[str, Set[str]] = {"company": set(), "deal": set(), "contact": set()}
    for row in rows:
        entity_type = text(row.get("entity_type"))
        status = text(row.get("action_status"))
        by_type[entity_type] = by_type.get(entity_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        discovered_ids.setdefault(entity_type, set()).add(norm_id(row.get("entity_id")))
    errors = [
        {
            "entity_type": text(row.get("entity_type")),
            "entity_id": text(row.get("entity_id")),
            "error": text(row.get("error")),
        }
        for row in rows
        if text(row.get("error"))
    ]
    unmatched_exclusions = {
        entity_type: sorted(ids - discovered_ids.get(entity_type, set()), key=int)
        for entity_type, ids in requested_exclusions.items()
        if ids - discovered_ids.get(entity_type, set())
    }
    return {
        "company_id": company_id,
        "founder_contact_id": founder_contact_id,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "total_rows": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "requested_exclusions": {key: sorted(value, key=int) for key, value in requested_exclusions.items()},
        "unmatched_exclusions": unmatched_exclusions,
        "timeline_comment": timeline_comment,
        "errors": errors[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BATCH reassignment by exact Bitrix company ID")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--founder-contact-id", required=True)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--exclude-company-ids", default="")
    parser.add_argument("--exclude-deal-ids", default="")
    parser.add_argument("--exclude-contact-ids", default="")
    parser.add_argument("--deal-originator-id", default="", help="Empty = all company deals")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--include-deal-contacts", default="true")
    parser.add_argument("--verify", default="true")
    parser.add_argument("--add-timeline-comment", default="true")
    parser.add_argument("--max-list-pages", default="100")
    parser.add_argument("--out", default="exports/reassign_by_company_id_batch_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_by_company_id_batch_summary.json")
    args = parser.parse_args()

    company_id = norm_id(args.company_id)
    founder_contact_id = norm_id(args.founder_contact_id)
    target_user_id = norm_id(args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    include_deal_contacts = parse_bool(args.include_deal_contacts, default=True)
    verify = parse_bool(args.verify, default=True)
    add_timeline_comment = parse_bool(args.add_timeline_comment, default=True)
    deal_originator_id = text(args.deal_originator_id)
    max_list_pages = int(args.max_list_pages)
    excluded_company_ids = parse_id_set(args.exclude_company_ids)
    excluded_deal_ids = parse_id_set(args.exclude_deal_ids)
    excluded_contact_ids = parse_id_set(args.exclude_contact_ids)

    if not company_id or not company_id.isdigit():
        raise ValueError("company_id must be a positive numeric Bitrix ID")
    if not founder_contact_id or not founder_contact_id.isdigit():
        raise ValueError("founder_contact_id must be a positive numeric Bitrix contact ID")
    if not target_user_id or not target_user_id.isdigit():
        raise ValueError("target_user_id must be a positive numeric Bitrix user ID")
    if max_list_pages <= 0:
        raise ValueError("max_list_pages must be positive")

    bx = Bitrix(os.getenv("BITRIX_WEBHOOK_URL", ""), timeout=int(os.getenv("REQUEST_TIMEOUT", "60")))
    target_user_name = bx.validate_user(target_user_id)

    print(f"MODE: {'DRY_RUN' if dry_run else 'WRITE'}")
    print(f"COMPANY_ID: {company_id}")
    print(f"FOUNDER_CONTACT_ID: {founder_contact_id}")
    print(f"TARGET_USER: {target_user_id} ({target_user_name})")
    print(f"DEAL_ORIGINATOR_ID: {deal_originator_id or 'ALL'}")
    print(
        "EXCLUSIONS: "
        f"companies={sorted(excluded_company_ids, key=int)}, "
        f"deals={sorted(excluded_deal_ids, key=int)}, "
        f"contacts={sorted(excluded_contact_ids, key=int)}"
    )

    company = bx.get_company(company_id)
    founder_contact = bx.get_contact(founder_contact_id)
    deals = bx.list_company_deals(
        company_id,
        include_closed_deals=include_closed_deals,
        deal_originator_id=deal_originator_id,
        max_pages=max_list_pages,
    )

    contact_ids = bx.list_direct_company_contact_ids(company_id, max_pages=max_list_pages)
    for deal in deals.values():
        primary_contact_id = norm_id(deal.get("CONTACT_ID"))
        if primary_contact_id:
            contact_ids.add(primary_contact_id)
    if include_deal_contacts and deals:
        contact_ids.update(bx.list_deal_contact_ids(list(deals)))
    contacts = bx.batch_get_entities("contact", list(contact_ids))

    rows = build_rows(
        company=company,
        deals=deals,
        contacts=contacts,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        dry_run=dry_run,
        excluded_company_ids=excluded_company_ids,
        excluded_deal_ids=excluded_deal_ids,
        excluded_contact_ids=excluded_contact_ids,
    )

    print(
        "DISCOVERED: "
        f"companies={sum(1 for row in rows if row['entity_type'] == 'company')}, "
        f"deals={sum(1 for row in rows if row['entity_type'] == 'deal')}, "
        f"contacts={sum(1 for row in rows if row['entity_type'] == 'contact')}, "
        f"excluded={sum(1 for row in rows if row['action_status'] == 'excluded')}, "
        f"total={len(rows)}"
    )

    timeline_comment: Dict[str, Any] = {
        "contact_id": founder_contact_id,
        "enabled": add_timeline_comment,
        "status": "dry_run_not_added" if dry_run else "not_attempted",
        "comment_id": "",
        "error": "",
    }

    if not dry_run:
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
            founder_contact_owner_id = norm_id(founder_contact.get("ASSIGNED_BY_ID"))
            if founder_contact_owner_id:
                previous_owner_ids.add(founder_contact_owner_id)
            user_labels = bx.get_user_labels(previous_owner_ids)
            comment_text = build_reassignment_comment(
                rows=rows,
                company_id=company_id,
                founder_contact_id=founder_contact_id,
                founder_contact_owner_id=founder_contact_owner_id,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                user_labels=user_labels,
            )
            try:
                timeline_comment["comment_id"] = bx.add_contact_timeline_comment(
                    founder_contact_id,
                    comment_text,
                )
                timeline_comment["status"] = "added"
            except Exception as exc:  # noqa: BLE001
                timeline_comment["status"] = "failed"
                timeline_comment["error"] = str(exc)[:2000]

    write_csv(args.out, rows)
    requested_exclusions = {
        "company": excluded_company_ids,
        "deal": excluded_deal_ids,
        "contact": excluded_contact_ids,
    }
    summary = make_summary(
        rows,
        company_id=company_id,
        founder_contact_id=founder_contact_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        dry_run=dry_run,
        requested_exclusions=requested_exclusions,
        timeline_comment=timeline_comment,
    )
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("errors") or timeline_comment.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
