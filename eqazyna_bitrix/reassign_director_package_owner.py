#!/usr/bin/env python3
"""Manual Bitrix owner reassignment by one e-Qazyna director/founder package.

Use case: admin writes a director/founder FIO and target Bitrix user ID; every
matched e-Qazyna deal, its company package and related director/contact cards
are moved to the target owner. This is deliberately separate from the automatic
load-based distribution logic.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

from .director import clean_director_value, director_identity_keys, extract_director_from_text
from .distribute_companies import SOURCE_RESPONSIBLE_IDS

DEFAULT_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
EQAZYNA_ORIGINATOR_ID = "EQAZYNA"
TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off"}
ALL_VALUES = {"", "all", "*", "все", "любой", "любая"}


class BitrixError(RuntimeError):
    pass


def s(value: Any) -> str:
    return str(value or "").strip()


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or s(value) == "":
        return default
    if isinstance(value, bool):
        return value
    text = s(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def parse_int(value: Any, field_name: str, *, default: int = 0) -> int:
    if value is None or s(value) == "":
        return default
    try:
        return int(s(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid integer {field_name}={value!r}") from exc


def normalize_webhook(url: str) -> str:
    url = s(url)
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


def normalize_id(value: Any) -> str:
    raw = s(value)
    if not raw:
        return ""
    try:
        return str(int(raw))
    except Exception:  # noqa: BLE001
        return raw


def sort_ids(values: Iterable[str]) -> List[str]:
    def key(value: str) -> Tuple[int, str]:
        return (int(value), "") if str(value).isdigit() else (10**18, str(value))

    return sorted({s(v) for v in values if s(v)}, key=key)


def crm_url_hint(entity_type: str, entity_id: Any, portal_base_url: str = "") -> str:
    portal = portal_base_url.rstrip("/") if portal_base_url else ""
    paths = {
        "deal": f"/crm/deal/details/{entity_id}/",
        "company": f"/crm/company/details/{entity_id}/",
        "contact": f"/crm/contact/details/{entity_id}/",
    }
    path = paths.get(entity_type, "")
    return (portal + path) if portal else path


def entity_title(entity_type: str, row: Dict[str, Any]) -> str:
    if entity_type == "contact":
        return contact_title(row) or s(row.get("ID"))
    return s(row.get("TITLE")) or s(row.get("NAME")) or s(row.get("ID"))


def contact_title(contact: Dict[str, Any]) -> str:
    return " ".join(s(x) for x in [contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME")] if s(x)).strip() or s(contact.get("TITLE"))


def director_candidate_from_comments(row: Dict[str, Any]) -> str:
    return clean_director_value(extract_director_from_text(s(row.get("COMMENTS"))))


def director_input_keys(raw_name: str) -> Set[str]:
    cleaned = clean_director_value(raw_name)
    if not cleaned:
        return set()
    return set(director_identity_keys(cleaned))


def name_token_count(raw_name: str) -> int:
    return len(re.findall(r"[A-ZА-ЯӘҒҚҢӨҰҮҺІЁ]+", s(raw_name), flags=re.IGNORECASE))


def director_matches(target_keys: Set[str], candidate_name: str) -> bool:
    if not target_keys or not candidate_name:
        return False
    return bool(target_keys.intersection(director_identity_keys(candidate_name)))


@dataclass
class MatchEvidence:
    matched_name: str = ""
    source: str = ""


@dataclass
class PackageSelection:
    deals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    companies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    contacts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evidence: Dict[Tuple[str, str], MatchEvidence] = field(default_factory=dict)
    unresolved_deals: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, entity_type: str, entity: Dict[str, Any], matched_name: str = "", source: str = "") -> None:
        entity_id = normalize_id(entity.get("ID"))
        if not entity_id:
            return
        if entity_type == "deal":
            self.deals[entity_id] = entity
        elif entity_type == "company":
            self.companies[entity_id] = entity
        elif entity_type == "contact":
            self.contacts[entity_id] = entity
        if matched_name or source:
            self.evidence[(entity_type, entity_id)] = MatchEvidence(matched_name=matched_name, source=source)

    def evidence_for(self, entity_type: str, entity_id: Any) -> MatchEvidence:
        return self.evidence.get((entity_type, normalize_id(entity_id)), MatchEvidence())


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = DEFAULT_TIMEOUT, polite_delay_seconds: float = 0.15) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.polite_delay_seconds = polite_delay_seconds
        self.session = requests.Session()
        self._user_cache: Dict[str, str] = {}
        self._user_object_cache: Dict[str, Dict[str, Any]] = {}
        self._contact_cache: Dict[str, Dict[str, Any]] = {}
        self._company_cache: Dict[str, Dict[str, Any]] = {}

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        response = self.session.post(self.base + method + ".json", json=payload or {}, timeout=self.timeout)
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:500]}") from exc
        if response.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:1200]}")
        if self.polite_delay_seconds:
            time.sleep(self.polite_delay_seconds)
        return data.get("result")

    def list_all(self, method: str, payload: Optional[Dict[str, Any]] = None, limit: int = 0) -> List[Dict[str, Any]]:
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
            output.extend(dict(item) for item in items)
            if limit and len(output) >= limit:
                return output[:limit]
            if next_start is None or next_start == "":
                break
            start = next_start
        return output

    def get_user(self, user_id: Any) -> Dict[str, Any]:
        uid = normalize_id(user_id)
        if not uid:
            return {}
        if uid in self._user_object_cache:
            return self._user_object_cache[uid]
        result = self.call("user.get", {"ID": uid})
        user = dict(result[0]) if isinstance(result, list) and result else {}
        self._user_object_cache[uid] = user
        return user

    def get_user_name(self, user_id: Any) -> str:
        uid = normalize_id(user_id)
        if not uid:
            return ""
        if uid in self._user_cache:
            return self._user_cache[uid]
        name = f"ID {uid}"
        try:
            user = self.get_user(uid)
            if user:
                full_name = " ".join(s(x) for x in [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")] if s(x)).strip()
                name = full_name or s(user.get("EMAIL")) or name
        except Exception:  # noqa: BLE001
            pass
        self._user_cache[uid] = name
        return name

    def validate_target_user_exists(self, user_id: Any) -> Dict[str, Any]:
        uid = normalize_id(user_id)
        user = self.get_user(uid)
        if not user:
            raise ValueError(f"target_user_id={uid} was not found in Bitrix user.get")
        active = user.get("ACTIVE")
        if s(active).lower() in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={uid} exists in Bitrix, but user is inactive")
        return user

    def get_contact(self, contact_id: Any) -> Dict[str, Any]:
        cid = normalize_id(contact_id)
        if not cid:
            return {}
        if cid not in self._contact_cache:
            try:
                self._contact_cache[cid] = self.call("crm.contact.get", {"id": cid}) or {}
            except Exception:  # noqa: BLE001
                self._contact_cache[cid] = {}
        return self._contact_cache[cid]

    def get_company(self, company_id: Any) -> Dict[str, Any]:
        cid = normalize_id(company_id)
        if not cid:
            return {}
        if cid not in self._company_cache:
            try:
                self._company_cache[cid] = self.call("crm.company.get", {"id": cid}) or {}
            except Exception:  # noqa: BLE001
                self._company_cache[cid] = {}
        return self._company_cache[cid]

    def list_deals(self, *, category_id: str, only_eqazyna: bool, include_closed: bool, limit: int = 0) -> List[Dict[str, Any]]:
        flt: Dict[str, Any] = {}
        if category_id and category_id.lower() not in ALL_VALUES:
            flt["CATEGORY_ID"] = category_id
        if only_eqazyna:
            flt["ORIGINATOR_ID"] = EQAZYNA_ORIGINATOR_ID
        if not include_closed:
            flt["CLOSED"] = "N"
        return self.list_all(
            "crm.deal.list",
            {
                "order": {"ID": "ASC"},
                "filter": flt,
                "select": [
                    "ID", "TITLE", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "CATEGORY_ID",
                    "STAGE_ID", "STAGE_SEMANTIC_ID", "CLOSED", "COMMENTS", "ORIGINATOR_ID",
                    "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY",
                ],
            },
            limit=limit,
        )

    def list_companies(self, *, only_eqazyna: bool, limit: int = 0) -> List[Dict[str, Any]]:
        flt: Dict[str, Any] = {}
        if only_eqazyna:
            flt["ORIGINATOR_ID"] = EQAZYNA_ORIGINATOR_ID
        return self.list_all(
            "crm.company.list",
            {
                "order": {"ID": "ASC"},
                "filter": flt,
                "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "COMMENTS", "ORIGINATOR_ID", "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY"],
            },
            limit=limit,
        )

    def company_contact_ids(self, company_id: Any) -> Set[str]:
        cid = normalize_id(company_id)
        if not cid:
            return set()
        try:
            result = self.call("crm.company.contact.items.get", {"id": cid})
        except Exception:  # noqa: BLE001
            return set()
        if not isinstance(result, list):
            return set()
        return {normalize_id(item.get("CONTACT_ID")) for item in result if normalize_id(item.get("CONTACT_ID"))}

    def deal_contact_ids(self, deal_id: Any) -> Set[str]:
        did = normalize_id(deal_id)
        if not did:
            return set()
        try:
            result = self.call("crm.deal.contact.items.get", {"id": did})
        except Exception:  # noqa: BLE001
            return set()
        if not isinstance(result, list):
            return set()
        return {normalize_id(item.get("CONTACT_ID")) for item in result if normalize_id(item.get("CONTACT_ID"))}

    def find_contacts_by_director_alias(self, director_raw: str, limit_per_token: int = 50) -> List[Dict[str, Any]]:
        target_keys = set(director_identity_keys(director_raw))
        if not target_keys:
            return []
        tokens: List[str] = []
        for key in target_keys:
            parts = key.split("|")
            for part in parts[1:]:
                if len(part) > 1 and part not in tokens:
                    tokens.append(part)
        candidates: Dict[str, Dict[str, Any]] = {}
        for token in tokens[:8]:
            for field_name in ("LAST_NAME", "NAME"):
                try:
                    result = self.list_all(
                        "crm.contact.list",
                        {
                            "order": {"ID": "ASC"},
                            "filter": {field_name: token},
                            "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME", "POST", "COMMENTS", "COMPANY_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY"],
                        },
                        limit=limit_per_token,
                    )
                except Exception:  # noqa: BLE001
                    continue
                for contact in result:
                    contact_id = normalize_id(contact.get("ID"))
                    if contact_id and director_matches(target_keys, contact_title(contact)):
                        candidates[contact_id] = contact
        return [candidates[key] for key in sort_ids(candidates.keys())]

    def update_owner(self, entity_type: str, entity_id: Any, target_user_id: str) -> None:
        methods = {
            "deal": "crm.deal.update",
            "company": "crm.company.update",
            "contact": "crm.contact.update",
        }
        method = methods[entity_type]
        self.call(method, {"id": normalize_id(entity_id), "fields": {"ASSIGNED_BY_ID": target_user_id}, "params": {"REGISTER_SONET_EVENT": "N"}})


def deal_director_match(
    *,
    bx: Bitrix,
    deal: Dict[str, Any],
    company_by_id: Dict[str, Dict[str, Any]],
    target_keys: Set[str],
) -> MatchEvidence:
    contact_id = normalize_id(deal.get("CONTACT_ID"))
    if contact_id:
        contact = bx.get_contact(contact_id)
        name = clean_director_value(contact_title(contact))
        if director_matches(target_keys, name):
            return MatchEvidence(matched_name=name, source="deal_contact")

    deal_name = director_candidate_from_comments(deal)
    if director_matches(target_keys, deal_name):
        return MatchEvidence(matched_name=deal_name, source="deal_comments")

    company_id = normalize_id(deal.get("COMPANY_ID"))
    company = company_by_id.get(company_id) or bx.get_company(company_id)
    if company:
        company_name = director_candidate_from_comments(company)
        if director_matches(target_keys, company_name):
            return MatchEvidence(matched_name=company_name, source="linked_company_comments")

    return MatchEvidence()


def build_package_selection(
    *,
    bx: Bitrix,
    director_name: str,
    only_eqazyna: bool,
    include_closed_deals: bool,
    deal_category_id: str,
    include_companies: bool,
    include_contacts: bool,
    include_deals: bool,
    include_orphan_companies: bool,
    include_company_contacts: bool,
    include_matching_director_contacts: bool,
    max_deals: int = 0,
    max_companies: int = 0,
) -> PackageSelection:
    target_keys = director_input_keys(director_name)
    if not target_keys:
        raise ValueError(f"Cannot build director identity key from director_name={director_name!r}")

    selected = PackageSelection()
    all_companies = bx.list_companies(only_eqazyna=only_eqazyna, limit=max_companies) if include_companies and include_orphan_companies else []
    company_by_id: Dict[str, Dict[str, Any]] = {normalize_id(company.get("ID")): company for company in all_companies if normalize_id(company.get("ID"))}

    company_match_ids: Set[str] = set()
    if include_companies and include_orphan_companies:
        for company in all_companies:
            company_id = normalize_id(company.get("ID"))
            name = director_candidate_from_comments(company)
            if company_id and director_matches(target_keys, name):
                company_match_ids.add(company_id)
                selected.add("company", company, matched_name=name, source="company_comments")

    all_deals = bx.list_deals(category_id=deal_category_id, only_eqazyna=only_eqazyna, include_closed=include_closed_deals, limit=max_deals)
    matched_deal_ids: Set[str] = set()
    linked_company_ids: Set[str] = set(company_match_ids)

    for deal in all_deals:
        deal_id = normalize_id(deal.get("ID"))
        company_id = normalize_id(deal.get("COMPANY_ID"))
        evidence = deal_director_match(bx=bx, deal=deal, company_by_id=company_by_id, target_keys=target_keys)
        if evidence.source:
            matched_deal_ids.add(deal_id)
            if company_id:
                linked_company_ids.add(company_id)
            if include_deals:
                selected.add("deal", deal, matched_name=evidence.matched_name, source=evidence.source)

    # If one deal/company in the package matched, every e-Qazyna deal of that company belongs to the same manual package.
    # This is intentional for the admin flow: package consistency is stronger than pretty partial movement.
    for deal in all_deals:
        deal_id = normalize_id(deal.get("ID"))
        company_id = normalize_id(deal.get("COMPANY_ID"))
        if include_deals and deal_id and company_id and company_id in linked_company_ids and deal_id not in selected.deals:
            selected.add("deal", deal, matched_name="", source="same_company_package")
            matched_deal_ids.add(deal_id)

    if include_companies:
        for company_id in sort_ids(linked_company_ids):
            company = company_by_id.get(company_id) or bx.get_company(company_id)
            if company:
                source = selected.evidence_for("company", company_id).source or "selected_deal_company"
                matched_name = selected.evidence_for("company", company_id).matched_name
                selected.add("company", company, matched_name=matched_name, source=source)

    contact_ids: Set[str] = set()
    if include_contacts:
        for deal in selected.deals.values():
            contact_id = normalize_id(deal.get("CONTACT_ID"))
            if contact_id:
                contact_ids.add(contact_id)
            for linked_contact_id in bx.deal_contact_ids(deal.get("ID")):
                contact_ids.add(linked_contact_id)
        if include_company_contacts:
            for company_id in selected.companies.keys():
                for contact_id in bx.company_contact_ids(company_id):
                    contact_ids.add(contact_id)
        for contact_id in sort_ids(contact_ids):
            contact = bx.get_contact(contact_id)
            if contact:
                selected.add("contact", contact, matched_name=contact_title(contact), source="linked_to_selected_package")
        if include_matching_director_contacts:
            for contact in bx.find_contacts_by_director_alias(director_name):
                selected.add("contact", contact, matched_name=contact_title(contact), source="director_alias_contact")

    return selected


def write_csv(path: str, rows: List[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_json(path: str, payload: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def action_row(
    *,
    entity_type: str,
    entity: Dict[str, Any],
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    evidence: MatchEvidence,
    portal_base_url: str,
) -> Dict[str, Any]:
    entity_id = normalize_id(entity.get("ID"))
    current_owner_id = normalize_id(entity.get("ASSIGNED_BY_ID"))
    action = "skip_already_target_owner" if current_owner_id == target_user_id else "dry_run_update" if dry_run else "updated"
    return {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_title": entity_title(entity_type, entity),
        "from_owner_id": current_owner_id,
        "to_owner_id": target_user_id,
        "to_owner_name": target_user_name,
        "matched_director_name": evidence.matched_name,
        "match_source": evidence.source,
        "company_id": normalize_id(entity.get("COMPANY_ID")),
        "contact_id": normalize_id(entity.get("CONTACT_ID")),
        "category_id": s(entity.get("CATEGORY_ID")),
        "stage_id": s(entity.get("STAGE_ID")),
        "closed": s(entity.get("CLOSED")),
        "originator_id": s(entity.get("ORIGINATOR_ID")),
        "origin_id": s(entity.get("ORIGIN_ID")),
        "url_hint": crm_url_hint(entity_type, entity_id, portal_base_url),
        "error": "",
    }


def run(args: argparse.Namespace) -> int:
    director_name = clean_director_value(args.director_name or args.founder_name)
    if not director_name:
        raise ValueError("director_name/founder_name is required")
    if name_token_count(director_name) < 2:
        raise ValueError("Укажите минимум фамилию + имя/инициал. Один токен слишком рискован для массового переноса.")

    target_user_id = normalize_id(args.target_user_id)
    if not target_user_id:
        raise ValueError("target_user_id is required")
    source_ids = {str(x) for x in SOURCE_RESPONSIBLE_IDS}
    if target_user_id in source_ids:
        raise ValueError(f"target_user_id={target_user_id} is a technical/source user ({','.join(sorted(source_ids))}); refusing to assign package to it")
    dry_run = parse_bool(args.dry_run, default=True)
    only_eqazyna = parse_bool(args.only_eqazyna, default=True)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    include_companies = parse_bool(args.include_companies, default=True)
    include_contacts = parse_bool(args.include_contacts, default=True)
    include_deals = parse_bool(args.include_deals, default=True)
    include_orphan_companies = parse_bool(args.include_orphan_companies, default=True)
    include_company_contacts = parse_bool(args.include_company_contacts, default=True)
    include_matching_director_contacts = parse_bool(args.include_matching_director_contacts, default=True)
    fail_if_no_deals = parse_bool(args.fail_if_no_deals, default=False)
    max_deals = parse_int(args.max_deals, "max_deals", default=0)
    max_companies = parse_int(args.max_companies, "max_companies", default=0)
    timeout = parse_int(os.getenv("REQUEST_TIMEOUT", "60"), "REQUEST_TIMEOUT", default=60)
    portal_base_url = s(args.portal_base_url or os.getenv("BITRIX_PORTAL_URL", ""))

    bx = Bitrix(BITRIX_WEBHOOK_URL, timeout=timeout)
    bx.validate_target_user_exists(target_user_id)
    target_user_name = bx.get_user_name(target_user_id)

    package = build_package_selection(
        bx=bx,
        director_name=director_name,
        only_eqazyna=only_eqazyna,
        include_closed_deals=include_closed_deals,
        deal_category_id=s(args.deal_category_id or "all"),
        include_companies=include_companies,
        include_contacts=include_contacts,
        include_deals=include_deals,
        include_orphan_companies=include_orphan_companies,
        include_company_contacts=include_company_contacts,
        include_matching_director_contacts=include_matching_director_contacts,
        max_deals=max_deals,
        max_companies=max_companies,
    )

    if fail_if_no_deals and not package.deals:
        raise ValueError(f"No e-Qazyna deals found for director_name={director_name!r}")
    if not package.deals and not package.companies and not package.contacts:
        raise ValueError(f"No CRM entities found for director_name={director_name!r}")

    rows: List[Dict[str, Any]] = []
    errors = 0
    updated = 0

    entity_groups: List[Tuple[str, Dict[str, Dict[str, Any]]]] = [
        ("company", package.companies),
        ("contact", package.contacts),
        ("deal", package.deals),
    ]
    for entity_type, entities in entity_groups:
        for entity_id in sort_ids(entities.keys()):
            entity = entities[entity_id]
            evidence = package.evidence_for(entity_type, entity_id)
            row = action_row(
                entity_type=entity_type,
                entity=entity,
                target_user_id=target_user_id,
                target_user_name=target_user_name,
                dry_run=dry_run,
                evidence=evidence,
                portal_base_url=portal_base_url,
            )
            if row["action"] in {"skip_already_target_owner", "dry_run_update"}:
                rows.append(row)
                continue
            try:
                bx.update_owner(entity_type, entity_id, target_user_id)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                row["action"] = "update_error"
                row["error"] = str(exc)
                errors += 1
            rows.append(row)

    fields = [
        "action", "entity_type", "entity_id", "entity_title", "from_owner_id", "to_owner_id", "to_owner_name",
        "matched_director_name", "match_source", "company_id", "contact_id", "category_id", "stage_id", "closed",
        "originator_id", "origin_id", "url_hint", "error",
    ]
    write_csv(args.out, rows, fields)
    write_json(args.json_out, {
        "director_name": director_name,
        "director_identity_keys": sorted(director_input_keys(director_name)),
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "only_eqazyna": only_eqazyna,
        "include_closed_deals": include_closed_deals,
        "deal_category_id": s(args.deal_category_id or "all"),
        "company_count": len(package.companies),
        "contact_count": len(package.contacts),
        "deal_count": len(package.deals),
        "action_rows": len(rows),
        "updated": updated,
        "errors": errors,
        "out": args.out,
        "json_out": args.json_out,
    })

    print("REASSIGN_DIRECTOR_PACKAGE_OWNER_DONE")
    print(json.dumps({
        "director_name": director_name,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "only_eqazyna": only_eqazyna,
        "include_closed_deals": include_closed_deals,
        "company_count": len(package.companies),
        "contact_count": len(package.contacts),
        "deal_count": len(package.deals),
        "action_rows": len(rows),
        "updated": updated,
        "errors": errors,
        "out": args.out,
        "json_out": args.json_out,
    }, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reassign one e-Qazyna director/founder package to a target Bitrix user")
    parser.add_argument("--director-name", default="", help="ФИО руководителя/учредителя. Минимум фамилия + имя/инициал")
    parser.add_argument("--founder-name", default="", help="Alias for --director-name")
    parser.add_argument("--target-user-id", required=True, help="Bitrix user ID of new responsible owner")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--only-eqazyna", default="true", help="true = move only e-Qazyna CRM entities")
    parser.add_argument("--include-closed-deals", default="true", help="true = include closed/lost deals too")
    parser.add_argument("--deal-category-id", default="all", help="Bitrix deal category. all = every category")
    parser.add_argument("--include-companies", default="true")
    parser.add_argument("--include-contacts", default="true")
    parser.add_argument("--include-deals", default="true")
    parser.add_argument("--include-orphan-companies", default="true", help="Also scan e-Qazyna companies whose COMMENTS contain this director, even if no deal matched first")
    parser.add_argument("--include-company-contacts", default="true", help="Move contacts linked to selected companies")
    parser.add_argument("--include-matching-director-contacts", default="true", help="Move contacts whose FIO matches the director/founder name")
    parser.add_argument("--allow-non-manager-target", default="true", help="Deprecated; ignored. target_user_id only has to exist in Bitrix")
    parser.add_argument("--fail-if-no-deals", default="false", help="true = fail when only company/contact matched but no deal was found")
    parser.add_argument("--max-deals", default="0", help="Safety scan limit for deals. 0 = no limit")
    parser.add_argument("--max-companies", default="0", help="Safety scan limit for companies. 0 = no limit")
    parser.add_argument("--portal-base-url", default="https://b24-izmquv.bitrix24.kz")
    parser.add_argument("--out", default="exports/reassign_director_package_owner_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_director_package_owner_summary.json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
