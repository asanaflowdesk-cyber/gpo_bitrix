#!/usr/bin/env python3
"""Batch reassignment of a complete Bitrix CRM portfolio.

The tool starts from every company, contact and deal currently assigned to the
source user, expands through CRM relations, and assigns the whole connected
package to the target user. Every deal is reopened in the first stage of its
pipeline and marked with the ``❗`` prefix. A service comment is added to each
related contact card so the handover remains visible in the CRM timeline.

The manager configuration is intentionally not used. Source and target users
are validated directly through Bitrix ``user.get``.
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
NONE_VALUES = {"", "none", "нет", "no", "0", "-"}
DEFAULT_FAILURE_REASON_FIELD = "UF_CRM_1779448756033"
DEAL_MARKER = "❗"

# Kept for backward-compatible imports used by the existing test suite and
# operational notes. The portfolio workflow clears this field instead of
# assigning a close reason.
CLOSE_REASON_OPTIONS: Dict[str, str] = {
    "400": "Дубль сделки",
    "394": "Клиент отказался",
    "402": "Не ведёт деятельность / компания неактивна",
    "386": "Не дозвонились",
    "396": "Не подходит по критериям",
    "392": "Нет данных организации",
    "398": "Ошибка данных",
    "404": "Проиграли аукцион",
    "390": "Уже работает с Евразией",
    "388": "Уже работает с конкурентом",
}

ENTITY_METHODS = {
    "company": {"get": "crm.company.get", "update": "crm.company.update"},
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update"},
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update"},
}

COMPANY_SELECT = ["ID", "TITLE", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY"]
CONTACT_SELECT = [
    "ID",
    "NAME",
    "LAST_NAME",
    "SECOND_NAME",
    "ASSIGNED_BY_ID",
    "COMPANY_ID",
    "DATE_CREATE",
    "DATE_MODIFY",
]
DEAL_SELECT = [
    "ID",
    "TITLE",
    "ASSIGNED_BY_ID",
    "COMPANY_ID",
    "CONTACT_ID",
    "CATEGORY_ID",
    "STAGE_ID",
    "CLOSED",
    DEFAULT_FAILURE_REASON_FIELD,
    "DATE_CREATE",
    "DATE_MODIFY",
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


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = text(value).lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def parse_int(value: Any, field_name: str, *, default: Optional[int] = None) -> Optional[int]:
    if value is None or text(value) == "":
        return default
    try:
        return int(text(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid integer {field_name}={value!r}") from exc


def parse_close_reason_id(value: Any) -> str:
    """Backward-compatible close-reason parser used by existing tests."""
    if value is None:
        return ""
    raw = text(value)
    if raw.lower() in NONE_VALUES:
        return ""
    reason_id = raw.split("-", 1)[0].strip()
    if reason_id not in CLOSE_REASON_OPTIONS:
        allowed = ", ".join(f"{key}={name}" for key, name in CLOSE_REASON_OPTIONS.items())
        raise ValueError(f"Invalid failure_reason_id={value!r}. Allowed: {allowed}")
    return reason_id


def close_reason_name(reason_id: str) -> str:
    return CLOSE_REASON_OPTIONS.get(text(reason_id), "")


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for position in range(0, len(items), size):
        yield items[position : position + size]


def add_select(params: List[Tuple[str, Any]], fields: Iterable[str]) -> None:
    for field in fields:
        params.append(("select[]", field))


def add_multi_filter(params: List[Tuple[str, Any]], field: str, values: Iterable[str]) -> None:
    clean = [norm_id(value) for value in values if norm_id(value)]
    if not clean:
        return
    if len(clean) == 1:
        params.append((f"filter[{field}]", clean[0]))
        return
    for value in clean:
        params.append((f"filter[{field}][]", value))


def entity_title(entity_type: str, entity: Dict[str, Any]) -> str:
    if entity_type in {"company", "deal"}:
        return text(entity.get("TITLE")) or f"{entity_type} #{entity.get('ID')}"
    parts = [text(entity.get("LAST_NAME")), text(entity.get("NAME")), text(entity.get("SECOND_NAME"))]
    return " ".join(part for part in parts if part) or f"contact #{entity.get('ID')}"


def marked_deal_title(title: Any) -> str:
    raw = text(title)
    if not raw:
        return DEAL_MARKER
    if raw.lstrip().startswith(DEAL_MARKER):
        return raw
    return f"{DEAL_MARKER} {raw}"


def new_stage_id(category_id: Any) -> str:
    normalized = norm_id(category_id)
    if normalized in {"", "0"}:
        return "NEW"
    return f"C{normalized}:NEW"


def crm_url_hint(entity_type: str, entity_id: Any, portal_base_url: str = "") -> str:
    portal = text(portal_base_url).rstrip("/")
    path_by_type = {
        "company": f"/crm/company/details/{entity_id}/",
        "contact": f"/crm/contact/details/{entity_id}/",
        "deal": f"/crm/deal/details/{entity_id}/",
    }
    path = path_by_type.get(entity_type, "")
    return f"{portal}{path}" if portal else path


class BitrixClient:
    def __init__(self, webhook_url: str, timeout: int = 60) -> None:
        webhook_url = text(webhook_url)
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL is empty")
        self.base_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = requests.Session()

    @staticmethod
    def _parse_response(method: str, response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:1000]}") from exc
        if response.status_code >= 400 or "error" in payload:
            raise BitrixError(f"{method}: {json.dumps(payload, ensure_ascii=False)[:2000]}")
        return payload

    def call_full(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Dict[str, Any]:
        response = self.session.post(
            self.base_url + method + ".json",
            data=list(params or []),
            timeout=self.timeout,
        )
        return self._parse_response(method, response)

    def call(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Any:
        return self.call_full(method, params).get("result")

    def call_json(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        response = self.session.post(
            self.base_url + method + ".json",
            json=payload or {},
            timeout=self.timeout,
        )
        return self._parse_response(method, response).get("result")

    def _paged_list(self, method: str, params_base: List[Tuple[str, Any]], limit: int = 0) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            params = list(params_base)
            params.append(("start", start))
            payload = self.call_full(method, params)
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

    def batch(self, commands: Dict[str, str], halt: bool = False) -> Dict[str, Any]:
        if not commands:
            return {"result": {}, "result_error": {}}
        params: List[Tuple[str, Any]] = [("halt", "1" if halt else "0")]
        for key, command in commands.items():
            params.append((f"cmd[{key}]", command))
        result = self.call_full("batch", params).get("result") or {}
        if not isinstance(result, dict):
            raise BitrixError(f"batch returned unexpected result: {result!r}")
        return result

    def validate_user(self, user_id: int, role: str) -> str:
        result = self.call_json("user.get", {"ID": str(user_id)})
        user = dict(result[0]) if isinstance(result, list) and result else {}
        if not user:
            raise ValueError(f"{role}_user_id={user_id} not found by user.get")
        if role == "target" and text(user.get("ACTIVE")).lower() in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={user_id} is inactive")
        parts = [text(user.get("LAST_NAME")), text(user.get("NAME")), text(user.get("SECOND_NAME"))]
        return " ".join(part for part in parts if part) or text(user.get("EMAIL")) or f"ID {user_id}"

    def get_user_labels(self, user_ids: Iterable[str]) -> Dict[str, str]:
        labels: Dict[str, str] = {}
        clean_ids = sorted({norm_id(value) for value in user_ids if norm_id(value)}, key=int)
        for user_id in clean_ids:
            try:
                result = self.call_json("user.get", {"ID": user_id})
                user = dict(result[0]) if isinstance(result, list) and result else {}
                parts = [text(user.get("LAST_NAME")), text(user.get("NAME")), text(user.get("SECOND_NAME"))]
                labels[user_id] = " ".join(part for part in parts if part) or f"ID {user_id}"
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: user.get failed for #{user_id}: {exc}", flush=True)
                labels[user_id] = f"ID {user_id}"
        return labels

    def list_assigned(self, entity_type: str, source_user_id: int) -> Dict[str, Dict[str, Any]]:
        method = {
            "company": "crm.company.list",
            "contact": "crm.contact.list",
            "deal": "crm.deal.list",
        }[entity_type]
        select = {
            "company": COMPANY_SELECT,
            "contact": CONTACT_SELECT,
            "deal": DEAL_SELECT,
        }[entity_type]
        params: List[Tuple[str, Any]] = [
            ("order[ID]", "ASC"),
            ("filter[ASSIGNED_BY_ID]", str(source_user_id)),
        ]
        add_select(params, select)
        rows = self._paged_list(method, params)
        return {norm_id(row.get("ID")): row for row in rows if norm_id(row.get("ID"))}

    def list_deals_by_relations(
        self,
        *,
        company_ids: Set[str],
        contact_ids: Set[str],
    ) -> Dict[str, Dict[str, Any]]:
        deals: Dict[str, Dict[str, Any]] = {}
        for field, values in (("COMPANY_ID", company_ids), ("CONTACT_ID", contact_ids)):
            ordered = sorted({norm_id(value) for value in values if norm_id(value)}, key=int)
            for part in chunked(ordered, 40):
                params: List[Tuple[str, Any]] = [("order[ID]", "ASC")]
                add_multi_filter(params, field, part)
                add_select(params, DEAL_SELECT)
                for row in self._paged_list("crm.deal.list", params):
                    deal_id = norm_id(row.get("ID"))
                    if deal_id:
                        deals[deal_id] = row
        return deals

    def batch_get_entities(self, entity_type: str, ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        ordered = sorted({norm_id(value) for value in ids if norm_id(value)}, key=int)
        method = ENTITY_METHODS[entity_type]["get"]
        for part in chunked(ordered, 50):
            commands = {
                f"g{index}": method + "?" + urlencode({"id": entity_id})
                for index, entity_id in enumerate(part, start=1)
            }
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            if batch_errors:
                details = "; ".join(
                    f"{entity_type} #{entity_id}: {batch_errors.get(f'g{index}')}"
                    for index, entity_id in enumerate(part, start=1)
                    if f"g{index}" in batch_errors
                )
                raise BitrixError(f"Failed to read {entity_type} records: {details}")
            missing: List[str] = []
            for index, entity_id in enumerate(part, start=1):
                row = batch_result.get(f"g{index}")
                if isinstance(row, dict) and row:
                    result[entity_id] = dict(row)
                else:
                    missing.append(entity_id)
            if missing:
                raise BitrixError(
                    f"Bitrix returned no data for {entity_type} IDs: {','.join(missing)}"
                )
        return result

    def batch_relation_ids(self, relation_type: str, ids: Iterable[str]) -> Dict[str, Set[str]]:
        """Return contact-company relations for all supplied entities."""
        ordered = sorted({norm_id(value) for value in ids if norm_id(value)}, key=int)
        result: Dict[str, Set[str]] = {entity_id: set() for entity_id in ordered}
        method = {
            "company_contacts": "crm.company.contact.items.get",
            "contact_companies": "crm.contact.company.items.get",
            "deal_contacts": "crm.deal.contact.items.get",
        }[relation_type]
        relation_field = "CONTACT_ID" if relation_type in {"company_contacts", "deal_contacts"} else "COMPANY_ID"
        for part in chunked(ordered, 50):
            commands = {
                f"r{index}": method + "?" + urlencode({"id": entity_id})
                for index, entity_id in enumerate(part, start=1)
            }
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            if batch_errors:
                details = "; ".join(
                    f"{relation_type} #{entity_id}: {batch_errors.get(f'r{index}')}"
                    for index, entity_id in enumerate(part, start=1)
                    if f"r{index}" in batch_errors
                )
                raise BitrixError(f"Failed to read CRM relations: {details}")
            for index, entity_id in enumerate(part, start=1):
                rows = batch_result.get(f"r{index}") or []
                for row in rows if isinstance(rows, list) else []:
                    related_id = norm_id((row or {}).get(relation_field) or (row or {}).get("ID"))
                    if related_id:
                        result[entity_id].add(related_id)
        return result

    def batch_update(self, rows: List[Dict[str, Any]], target_user_id: int) -> None:
        pending = [row for row in rows if row.get("action") == "planned_update"]
        for part in chunked(pending, 50):
            commands: Dict[str, str] = {}
            key_to_row: Dict[str, Dict[str, Any]] = {}
            for index, row in enumerate(part, start=1):
                entity_type = text(row.get("entity_type"))
                entity_id = norm_id(row.get("entity_id"))
                params: List[Tuple[str, Any]] = [
                    ("id", entity_id),
                    ("fields[ASSIGNED_BY_ID]", str(target_user_id)),
                    ("params[REGISTER_SONET_EVENT]", "N"),
                ]
                if entity_type == "deal":
                    params.extend(
                        [
                            ("fields[STAGE_ID]", text(row.get("target_stage_id"))),
                            ("fields[TITLE]", text(row.get("target_title"))),
                            (f"fields[{DEFAULT_FAILURE_REASON_FIELD}]", ""),
                        ]
                    )
                key = f"u{index}"
                commands[key] = ENTITY_METHODS[entity_type]["update"] + "?" + urlencode(params)
                key_to_row[key] = row

            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, row in key_to_row.items():
                if key in batch_errors:
                    row["action"] = "update_failed"
                    row["error"] = json.dumps(batch_errors[key], ensure_ascii=False)[:1200]
                else:
                    row["action"] = "update_sent"
                    row["update_result"] = json.dumps(batch_result.get(key), ensure_ascii=False)[:500]

    def batch_verify(self, rows: List[Dict[str, Any]], target_user_id: int) -> None:
        candidates = [row for row in rows if row.get("action") in {"update_sent", "already_target_planned"}]
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
                    row["action"] = "verify_failed"
                    row["error"] = json.dumps(batch_errors[key], ensure_ascii=False)[:1200]
                    continue
                entity = batch_result.get(key) or {}
                final_owner = norm_id(entity.get("ASSIGNED_BY_ID"))
                row["verified_owner_id"] = final_owner
                if final_owner != str(target_user_id):
                    row["action"] = "owner_not_changed"
                    row["error"] = f"verified_owner_id={final_owner}; target_user_id={target_user_id}"
                    continue
                if row.get("entity_type") == "deal":
                    final_stage = text(entity.get("STAGE_ID"))
                    final_title = text(entity.get("TITLE"))
                    row["verified_stage_id"] = final_stage
                    row["verified_title"] = final_title
                    problems: List[str] = []
                    if final_stage != text(row.get("target_stage_id")):
                        problems.append(f"stage={final_stage}")
                    if final_title != text(row.get("target_title")):
                        problems.append(f"title={final_title!r}")
                    if problems:
                        row["action"] = "deal_reset_not_verified"
                        row["error"] = "; ".join(problems)
                        continue
                row["action"] = "updated_and_verified"

    def batch_add_contact_comments(self, contact_ids: Iterable[str], comment: str) -> Dict[str, Dict[str, str]]:
        results: Dict[str, Dict[str, str]] = {}
        ordered = sorted({norm_id(value) for value in contact_ids if norm_id(value)}, key=int)
        for part in chunked(ordered, 50):
            commands: Dict[str, str] = {}
            key_to_contact: Dict[str, str] = {}
            for index, contact_id in enumerate(part, start=1):
                key = f"c{index}"
                params = [
                    ("fields[ENTITY_ID]", contact_id),
                    ("fields[ENTITY_TYPE]", "contact"),
                    ("fields[COMMENT]", comment),
                ]
                commands[key] = "crm.timeline.comment.add?" + urlencode(params)
                key_to_contact[key] = contact_id
            payload = self.batch(commands)
            batch_result = payload.get("result") or {}
            batch_errors = payload.get("result_error") or {}
            for key, contact_id in key_to_contact.items():
                if key in batch_errors:
                    results[contact_id] = {
                        "status": "comment_failed",
                        "comment_id": "",
                        "error": json.dumps(batch_errors[key], ensure_ascii=False)[:1200],
                    }
                else:
                    results[contact_id] = {
                        "status": "comment_added",
                        "comment_id": text(batch_result.get(key)),
                        "error": "",
                    }
        return results


class PortfolioDiscovery:
    def __init__(self, client: BitrixClient, source_user_id: int, max_entities: int) -> None:
        self.client = client
        self.source_user_id = source_user_id
        self.max_entities = max_entities
        self.companies: Dict[str, Dict[str, Any]] = {}
        self.contacts: Dict[str, Dict[str, Any]] = {}
        self.deals: Dict[str, Dict[str, Any]] = {}
        self.company_ids: Set[str] = set()
        self.contact_ids: Set[str] = set()
        self.deal_ids: Set[str] = set()
        self.processed_company_relations: Set[str] = set()
        self.processed_contact_relations: Set[str] = set()
        self.processed_deal_relations: Set[str] = set()
        self.searched_company_deals: Set[str] = set()
        self.searched_contact_deals: Set[str] = set()

    def _guard_size(self) -> None:
        total = len(self.company_ids) + len(self.contact_ids) + len(self.deal_ids)
        if total > self.max_entities:
            raise RuntimeError(
                f"Portfolio expanded to {total} entities, exceeding max_entities={self.max_entities}. "
                "Stop and inspect relations before a mass write."
            )

    def _ingest_deals(self, rows: Dict[str, Dict[str, Any]]) -> bool:
        changed = False
        for deal_id, deal in rows.items():
            if deal_id not in self.deal_ids:
                self.deal_ids.add(deal_id)
                changed = True
            self.deals[deal_id] = deal
            company_id = norm_id(deal.get("COMPANY_ID"))
            contact_id = norm_id(deal.get("CONTACT_ID"))
            if company_id and company_id not in self.company_ids:
                self.company_ids.add(company_id)
                changed = True
            if contact_id and contact_id not in self.contact_ids:
                self.contact_ids.add(contact_id)
                changed = True
        return changed

    def discover(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        print("[1/4] Reading source-owned CRM entities...", flush=True)
        self.companies = self.client.list_assigned("company", self.source_user_id)
        self.contacts = self.client.list_assigned("contact", self.source_user_id)
        self.deals = self.client.list_assigned("deal", self.source_user_id)
        self.company_ids.update(self.companies)
        self.contact_ids.update(self.contacts)
        self.deal_ids.update(self.deals)
        self._ingest_deals(self.deals)
        for contact in self.contacts.values():
            company_id = norm_id(contact.get("COMPANY_ID"))
            if company_id:
                self.company_ids.add(company_id)
        self._guard_size()

        print(
            f"Seeds: companies={len(self.company_ids)}, contacts={len(self.contact_ids)}, deals={len(self.deal_ids)}",
            flush=True,
        )
        print("[2/4] Expanding company/contact/deal relations...", flush=True)

        for iteration in range(1, 11):
            changed = False

            new_company_relations = self.company_ids - self.processed_company_relations
            if new_company_relations:
                relation_map = self.client.batch_relation_ids("company_contacts", new_company_relations)
                self.processed_company_relations.update(new_company_relations)
                for related in relation_map.values():
                    before = len(self.contact_ids)
                    self.contact_ids.update(related)
                    changed = changed or len(self.contact_ids) != before

            new_contact_relations = self.contact_ids - self.processed_contact_relations
            if new_contact_relations:
                relation_map = self.client.batch_relation_ids("contact_companies", new_contact_relations)
                self.processed_contact_relations.update(new_contact_relations)
                for related in relation_map.values():
                    before = len(self.company_ids)
                    self.company_ids.update(related)
                    changed = changed or len(self.company_ids) != before

            new_company_deal_search = self.company_ids - self.searched_company_deals
            new_contact_deal_search = self.contact_ids - self.searched_contact_deals
            if new_company_deal_search or new_contact_deal_search:
                found = self.client.list_deals_by_relations(
                    company_ids=new_company_deal_search,
                    contact_ids=new_contact_deal_search,
                )
                self.searched_company_deals.update(new_company_deal_search)
                self.searched_contact_deals.update(new_contact_deal_search)
                changed = self._ingest_deals(found) or changed

            new_deal_relations = self.deal_ids - self.processed_deal_relations
            if new_deal_relations:
                relation_map = self.client.batch_relation_ids("deal_contacts", new_deal_relations)
                self.processed_deal_relations.update(new_deal_relations)
                for related in relation_map.values():
                    before = len(self.contact_ids)
                    self.contact_ids.update(related)
                    changed = changed or len(self.contact_ids) != before

            self._guard_size()
            print(
                f"Relation pass {iteration}: companies={len(self.company_ids)}, "
                f"contacts={len(self.contact_ids)}, deals={len(self.deal_ids)}",
                flush=True,
            )
            if not changed:
                break
        else:
            raise RuntimeError("CRM relation expansion did not stabilize after 10 passes")

        print("[3/4] Loading complete entity records...", flush=True)
        self.companies.update(self.client.batch_get_entities("company", self.company_ids))
        self.contacts.update(self.client.batch_get_entities("contact", self.contact_ids))
        self.deals.update(self.client.batch_get_entities("deal", self.deal_ids))
        return self.companies, self.contacts, self.deals


def build_rows(
    *,
    companies: Dict[str, Dict[str, Any]],
    contacts: Dict[str, Dict[str, Any]],
    deals: Dict[str, Dict[str, Any]],
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    dry_run: bool,
    portal_base_url: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def common(entity_type: str, entity: Dict[str, Any]) -> Dict[str, Any]:
        before_owner = norm_id(entity.get("ASSIGNED_BY_ID"))
        return {
            "entity_type": entity_type,
            "entity_id": norm_id(entity.get("ID")),
            "entity_title": entity_title(entity_type, entity),
            "source_user_id": source_user_id,
            "source_user_name": source_user_name,
            "before_owner_id": before_owner,
            "target_user_id": target_user_id,
            "target_user_name": target_user_name,
            "dry_run": str(dry_run).lower(),
            "action": "already_target_planned" if before_owner == str(target_user_id) else "planned_update",
            "error": "",
            "update_result": "",
            "verified_owner_id": "",
            "crm_url_hint": crm_url_hint(entity_type, entity.get("ID"), portal_base_url),
        }

    for company_id in sorted(companies, key=int):
        rows.append(common("company", companies[company_id]))
    for contact_id in sorted(contacts, key=int):
        rows.append(common("contact", contacts[contact_id]))
    for deal_id in sorted(deals, key=int):
        deal = deals[deal_id]
        row = common("deal", deal)
        row.update(
            {
                "company_id": norm_id(deal.get("COMPANY_ID")),
                "contact_id": norm_id(deal.get("CONTACT_ID")),
                "category_id": norm_id(deal.get("CATEGORY_ID")),
                "before_stage_id": text(deal.get("STAGE_ID")),
                "target_stage_id": new_stage_id(deal.get("CATEGORY_ID")),
                "before_title": text(deal.get("TITLE")),
                "target_title": marked_deal_title(deal.get("TITLE")),
                "before_failure_reason": text(deal.get(DEFAULT_FAILURE_REASON_FIELD)),
                "verified_stage_id": "",
                "verified_title": "",
            }
        )
        # Even if the deal already belongs to the target user, it still has to
        # be reopened, marked and stripped of the old failure reason.
        requires_deal_reset = any(
            [
                row["before_owner_id"] != str(target_user_id),
                row["before_stage_id"] != row["target_stage_id"],
                row["before_title"] != row["target_title"],
                bool(row["before_failure_reason"]),
            ]
        )
        row["action"] = "planned_update" if requires_deal_reset else "already_target_planned"
        rows.append(row)
    return rows


def build_timeline_comment(
    *,
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    previous_owner_labels: List[str],
    company_count: int,
    deal_count: int,
    contact_count: int,
) -> str:
    previous = ", ".join(previous_owner_labels) if previous_owner_labels else f"{source_user_name} (ID {source_user_id})"
    return (
        "Служебная отметка о перераспределении.\n\n"
        f"Пакет клиента передан от {source_user_name} (ID {source_user_id}) "
        f"новому ответственному {target_user_name} (ID {target_user_id}).\n"
        f"Предыдущие ответственные связанных элементов: {previous}.\n\n"
        f"Переназначено: компаний — {company_count}, сделок — {deal_count}, контактов — {contact_count}.\n"
        f"Все сделки возвращены на стадию «Новая» и отмечены символом {DEAL_MARKER}."
    )


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type",
        "entity_id",
        "entity_title",
        "source_user_id",
        "source_user_name",
        "before_owner_id",
        "target_user_id",
        "target_user_name",
        "dry_run",
        "action",
        "error",
        "update_result",
        "verified_owner_id",
        "company_id",
        "contact_id",
        "category_id",
        "before_stage_id",
        "target_stage_id",
        "verified_stage_id",
        "before_title",
        "target_title",
        "verified_title",
        "before_failure_reason",
        "comment_id",
        "crm_url_hint",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> int:
    source_user_id = parse_int(args.source_user_id, "source_user_id")
    target_user_id = parse_int(args.target_user_id, "target_user_id")
    if not source_user_id or not target_user_id:
        raise ValueError("source_user_id and target_user_id are required")
    if source_user_id == target_user_id:
        raise ValueError("source_user_id and target_user_id must be different")

    dry_run = parse_bool(args.dry_run, default=True)
    max_entities = parse_int(args.max_entities, "max_entities", default=5000) or 5000
    timeout = parse_int(os.getenv("REQUEST_TIMEOUT", "60"), "REQUEST_TIMEOUT", default=60) or 60
    portal_base_url = text(args.portal_base_url or os.getenv("BITRIX_PORTAL_URL", ""))
    out_path = Path(args.out)

    client = BitrixClient(os.environ.get("BITRIX_WEBHOOK_URL", ""), timeout=timeout)
    source_user_name = client.validate_user(source_user_id, "source")
    target_user_name = client.validate_user(target_user_id, "target")

    print(
        f"MODE={'DRY_RUN' if dry_run else 'WRITE'} / {source_user_name} ({source_user_id}) "
        f"→ {target_user_name} ({target_user_id})",
        flush=True,
    )

    discovery = PortfolioDiscovery(client, source_user_id, max_entities=max_entities)
    companies, contacts, deals = discovery.discover()
    rows = build_rows(
        companies=companies,
        contacts=contacts,
        deals=deals,
        source_user_id=source_user_id,
        source_user_name=source_user_name,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        dry_run=dry_run,
        portal_base_url=portal_base_url,
    )

    if not rows:
        write_csv(out_path, rows)
        print("No CRM entities found for the source user.", flush=True)
        return 0

    print(
        f"[4/4] Package ready: companies={len(companies)}, contacts={len(contacts)}, deals={len(deals)}",
        flush=True,
    )

    if dry_run:
        for row in rows:
            row["action"] = "dry_run_update" if row["action"] == "planned_update" else "dry_run_already_target"
        write_csv(out_path, rows)
        print(f"DRY_RUN_COMPLETE out={out_path}", flush=True)
        return 0

    client.batch_update(rows, target_user_id)
    client.batch_verify(rows, target_user_id)

    transfer_error_actions = {
        "update_failed",
        "verify_failed",
        "owner_not_changed",
        "deal_reset_not_verified",
    }
    transfer_errors = [row for row in rows if row.get("action") in transfer_error_actions]

    # Do not write a misleading "package transferred" comment if at least one
    # entity failed to move or a deal failed to reset.
    if not transfer_errors:
        owner_ids = {text(row.get("before_owner_id")) for row in rows if text(row.get("before_owner_id"))}
        owner_labels = client.get_user_labels(owner_ids)
        previous_owner_labels = [
            f"{owner_labels[user_id]} (ID {user_id})"
            for user_id in sorted(owner_labels, key=int)
        ]
        comment = build_timeline_comment(
            source_user_id=source_user_id,
            source_user_name=source_user_name,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            previous_owner_labels=previous_owner_labels,
            company_count=len(companies),
            deal_count=len(deals),
            contact_count=len(contacts),
        )

        comment_results = client.batch_add_contact_comments(contacts.keys(), comment)
        rows_by_contact = {
            norm_id(row.get("entity_id")): row
            for row in rows
            if row.get("entity_type") == "contact"
        }
        for contact_id, result in comment_results.items():
            row = rows_by_contact.get(contact_id)
            if row is not None:
                row["comment_id"] = result.get("comment_id", "")
                if result.get("status") == "comment_failed":
                    row["action"] = "comment_failed"
                    row["error"] = result.get("error", "")
    else:
        print(
            f"Timeline comments skipped because transfer has {len(transfer_errors)} error(s).",
            flush=True,
        )

    write_csv(out_path, rows)

    errors = [row for row in rows if row.get("action") in {
        "update_failed",
        "verify_failed",
        "owner_not_changed",
        "deal_reset_not_verified",
        "comment_failed",
    }]
    updated = sum(1 for row in rows if row.get("action") == "updated_and_verified")
    print("REASSIGN_CRM_OWNER_DONE", flush=True)
    print(f"companies={len(companies)} contacts={len(contacts)} deals={len(deals)}", flush=True)
    print(f"updated_and_verified={updated} errors={len(errors)}", flush=True)
    print(f"out={out_path}", flush=True)
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reassign a complete Bitrix CRM portfolio and reopen all related deals"
    )
    parser.add_argument("--source-user-id", required=True, help="Current portfolio owner ID")
    parser.add_argument("--target-user-id", required=True, help="New portfolio owner ID")
    parser.add_argument("--dry-run", default="true", help="true = plan only, false = write to Bitrix")
    parser.add_argument(
        "--max-entities",
        default="5000",
        help="Hard safety cap for companies + contacts + deals discovered through relations",
    )
    parser.add_argument("--portal-base-url", default="https://b24-izmquv.bitrix24.kz")
    parser.add_argument("--out", default="exports/reassign_crm_owner_log.csv")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
