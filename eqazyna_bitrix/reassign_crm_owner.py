"""One-time Bitrix CRM owner reassignment tool.

Moves CRM entities from one responsible user to another with a dry-run first.
Intended for rare staff handover cases: companies/contacts/leads, optionally deals.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off"}


@dataclass(frozen=True)
class EntitySpec:
    key: str
    title: str
    list_method: str
    update_method: str
    id_field: str = "ID"
    title_fields: Tuple[str, ...] = ("TITLE", "NAME")
    select_fields: Tuple[str, ...] = ("ID", "TITLE", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "CONTACT_ID", "CATEGORY_ID", "STAGE_ID", "CLOSED")


ENTITY_SPECS: Dict[str, EntitySpec] = {
    "companies": EntitySpec(
        key="companies",
        title="Companies",
        list_method="crm.company.list",
        update_method="crm.company.update",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "contacts": EntitySpec(
        key="contacts",
        title="Contacts",
        list_method="crm.contact.list",
        update_method="crm.contact.update",
        title_fields=("FULL_NAME", "NAME", "LAST_NAME"),
        select_fields=("ID", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "leads": EntitySpec(
        key="leads",
        title="Leads",
        list_method="crm.lead.list",
        update_method="crm.lead.update",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "STATUS_SEMANTIC_ID", "COMPANY_ID", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "deals": EntitySpec(
        key="deals",
        title="Deals",
        list_method="crm.deal.list",
        update_method="crm.deal.update",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "CATEGORY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CLOSED", "COMPANY_ID", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
}


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in TRUE_VALUES:
        return True
    if s in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def parse_int(value: Any, field_name: str, *, default: Optional[int] = None) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except Exception as exc:
        raise ValueError(f"Invalid integer {field_name}={value!r}") from exc


class BitrixClient:
    def __init__(self, webhook_url: str, timeout: int = 60, sleep_seconds: float = 0.15) -> None:
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL is empty")
        self.base_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()

    def call(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Any:
        url = self.base_url + method + ".json"
        response = self.session.post(url, data=list(params or []), timeout=self.timeout)
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        try:
            payload = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError(f"Bitrix returned non-JSON response for {method}: {response.text[:500]}")
        if response.status_code >= 400 or "error" in payload:
            raise RuntimeError(f"Bitrix API error in {method}: status={response.status_code}, payload={payload}")
        return payload.get("result")

    def list_entities(
        self,
        spec: EntitySpec,
        source_user_id: int,
        *,
        include_closed_deals: bool = False,
        deal_category_id: str = "all",
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            params: List[Tuple[str, Any]] = [
                ("order[ID]", "ASC"),
                ("filter[ASSIGNED_BY_ID]", str(source_user_id)),
            ]
            if spec.key == "deals":
                if deal_category_id and str(deal_category_id).lower() not in {"all", ""}:
                    params.append(("filter[CATEGORY_ID]", str(deal_category_id)))
                # Filtering CLOSED in Bitrix is not always reliable across portals; list then skip.
            for field in spec.select_fields:
                params.append(("select[]", field))
            if start:
                params.append(("start", start))
            result = self.call(spec.list_method, params)
            if isinstance(result, dict) and "items" in result:
                items = result.get("items") or []
            else:
                items = result or []
            for item in items:
                if spec.key == "deals" and not include_closed_deals and str(item.get("CLOSED", "")).upper() == "Y":
                    continue
                rows.append(dict(item))
                if limit and len(rows) >= limit:
                    return rows
            if isinstance(result, dict):
                next_start = result.get("next")
            else:
                next_start = None
            # Bitrix also sometimes puts next at payload top-level, but not with this wrapper.
            if not next_start:
                break
            start = next_start
        return rows

    def update_owner(self, spec: EntitySpec, entity_id: str, target_user_id: int) -> Any:
        params = [
            ("id", str(entity_id)),
            ("fields[ASSIGNED_BY_ID]", str(target_user_id)),
        ]
        return self.call(spec.update_method, params)

    def get_user_name(self, user_id: int) -> str:
        try:
            result = self.call("user.get", [("ID", str(user_id))])
            if isinstance(result, list) and result:
                row = result[0]
            elif isinstance(result, dict):
                row = result
            else:
                return str(user_id)
            parts = [row.get("LAST_NAME"), row.get("NAME"), row.get("SECOND_NAME")]
            name = " ".join(str(x).strip() for x in parts if x and str(x).strip())
            return name or str(user_id)
        except Exception:
            return str(user_id)


def entity_title(spec: EntitySpec, row: Dict[str, Any]) -> str:
    if spec.key == "contacts":
        parts = [row.get("LAST_NAME"), row.get("NAME"), row.get("SECOND_NAME")]
        name = " ".join(str(x).strip() for x in parts if x and str(x).strip())
        if name:
            return name
    for field in spec.title_fields:
        value = row.get(field)
        if value:
            return str(value)
    return f"{spec.key}:{row.get('ID')}"


def crm_url_hint(entity_type: str, entity_id: Any, portal_base_url: str = "") -> str:
    portal = portal_base_url.rstrip("/") if portal_base_url else ""
    paths = {
        "companies": f"/crm/company/details/{entity_id}/",
        "contacts": f"/crm/contact/details/{entity_id}/",
        "leads": f"/crm/lead/details/{entity_id}/",
        "deals": f"/crm/deal/details/{entity_id}/",
    }
    path = paths.get(entity_type, "")
    return (portal + path) if portal else path


def selected_entities(args: argparse.Namespace) -> List[str]:
    keys: List[str] = []
    if parse_bool(args.include_companies, default=True):
        keys.append("companies")
    if parse_bool(args.include_contacts, default=True):
        keys.append("contacts")
    if parse_bool(args.include_leads, default=True):
        keys.append("leads")
    if parse_bool(args.include_deals, default=False):
        keys.append("deals")
    return keys


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type",
        "entity_id",
        "entity_title",
        "source_user_id",
        "source_user_name",
        "target_user_id",
        "target_user_name",
        "action",
        "dry_run",
        "error",
        "company_id",
        "contact_id",
        "category_id",
        "stage_id",
        "closed",
        "crm_url_hint",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run(args: argparse.Namespace) -> int:
    dry_run = parse_bool(args.dry_run, default=True)
    source_user_id = parse_int(args.source_user_id, "source_user_id")
    target_user_id = parse_int(args.target_user_id, "target_user_id")
    if not source_user_id or not target_user_id:
        raise ValueError("source_user_id and target_user_id are required")
    if source_user_id == target_user_id:
        raise ValueError("source_user_id and target_user_id must be different")

    limit = parse_int(args.limit, "limit", default=0) or 0
    include_closed_deals = parse_bool(args.include_closed_deals, default=False)
    portal_base_url = args.portal_base_url or os.getenv("BITRIX_PORTAL_URL", "")
    timeout = parse_int(os.getenv("REQUEST_TIMEOUT", "60"), "REQUEST_TIMEOUT", default=60) or 60
    client = BitrixClient(os.environ.get("BITRIX_WEBHOOK_URL", ""), timeout=timeout)

    source_user_name = client.get_user_name(source_user_id)
    target_user_name = client.get_user_name(target_user_id)

    rows_out: List[Dict[str, Any]] = []
    total_found = 0
    total_updated = 0
    total_errors = 0

    per_entity_limit = limit
    for key in selected_entities(args):
        spec = ENTITY_SPECS[key]
        try:
            items = client.list_entities(
                spec,
                source_user_id,
                include_closed_deals=include_closed_deals,
                deal_category_id=str(args.deal_category_id or "all"),
                limit=per_entity_limit,
            )
        except Exception as exc:
            total_errors += 1
            rows_out.append({
                "entity_type": key,
                "entity_id": "",
                "entity_title": "",
                "source_user_id": source_user_id,
                "source_user_name": source_user_name,
                "target_user_id": target_user_id,
                "target_user_name": target_user_name,
                "action": "list_error",
                "dry_run": dry_run,
                "error": str(exc),
            })
            continue

        for item in items:
            total_found += 1
            entity_id = str(item.get("ID") or "")
            base = {
                "entity_type": key,
                "entity_id": entity_id,
                "entity_title": entity_title(spec, item),
                "source_user_id": source_user_id,
                "source_user_name": source_user_name,
                "target_user_id": target_user_id,
                "target_user_name": target_user_name,
                "dry_run": dry_run,
                "company_id": item.get("COMPANY_ID", ""),
                "contact_id": item.get("CONTACT_ID", ""),
                "category_id": item.get("CATEGORY_ID", ""),
                "stage_id": item.get("STAGE_ID", item.get("STATUS_ID", "")),
                "closed": item.get("CLOSED", ""),
                "crm_url_hint": crm_url_hint(key, entity_id, portal_base_url),
            }
            if not entity_id:
                total_errors += 1
                rows_out.append({**base, "action": "skip_no_id", "error": "Entity ID is empty"})
                continue
            if dry_run:
                rows_out.append({**base, "action": "dry_run_update", "error": ""})
                continue
            try:
                client.update_owner(spec, entity_id, target_user_id)
                total_updated += 1
                rows_out.append({**base, "action": "updated", "error": ""})
            except Exception as exc:
                total_errors += 1
                rows_out.append({**base, "action": "update_error", "error": str(exc)})

    out_path = Path(args.out)
    write_csv(out_path, rows_out)

    print("REASSIGN_CRM_OWNER_DONE")
    print(f"source_user_id={source_user_id} source_user_name={source_user_name}")
    print(f"target_user_id={target_user_id} target_user_name={target_user_name}")
    print(f"dry_run={dry_run}")
    print(f"entities={','.join(selected_entities(args))}")
    print(f"found={total_found}")
    print(f"updated={total_updated}")
    print(f"errors={total_errors}")
    print(f"out={out_path}")
    return 1 if total_errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reassign Bitrix CRM entities from one responsible user to another")
    parser.add_argument("--source-user-id", required=True, help="Current responsible user ID")
    parser.add_argument("--target-user-id", required=True, help="New responsible user ID")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--include-companies", default="true")
    parser.add_argument("--include-contacts", default="true")
    parser.add_argument("--include-leads", default="true")
    parser.add_argument("--include-deals", default="false")
    parser.add_argument("--include-closed-deals", default="false")
    parser.add_argument("--deal-category-id", default="all")
    parser.add_argument("--limit", default="0", help="Max rows per entity type; 0 = no limit")
    parser.add_argument("--portal-base-url", default="https://b24-izmquv.bitrix24.kz")
    parser.add_argument("--out", default="exports/reassign_crm_owner_log.csv")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
