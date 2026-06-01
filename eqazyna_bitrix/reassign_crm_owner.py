"""One-time Bitrix CRM owner reassignment tool.

Moves CRM entities from one responsible user to another with a dry-run first.
Intended for rare staff handover cases: companies/contacts/leads, optionally deals.

This version supports safe manual slices:
- limit / max-total to move only a selected amount;
- deal stage filter for deals and for company/contact selection by related deal stage;
- lead status filter for leads.
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests


TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off"}
ALL_VALUES = {"", "all", "*", "все", "любой", "любая"}


@dataclass(frozen=True)
class EntitySpec:
    key: str
    title: str
    list_method: str
    update_method: str
    get_method: str
    id_field: str = "ID"
    title_fields: Tuple[str, ...] = ("TITLE", "NAME")
    select_fields: Tuple[str, ...] = (
        "ID", "TITLE", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID",
        "COMPANY_ID", "CONTACT_ID", "CATEGORY_ID", "STAGE_ID", "CLOSED",
    )


ENTITY_SPECS: Dict[str, EntitySpec] = {
    "companies": EntitySpec(
        key="companies",
        title="Companies",
        list_method="crm.company.list",
        update_method="crm.company.update",
        get_method="crm.company.get",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "contacts": EntitySpec(
        key="contacts",
        title="Contacts",
        list_method="crm.contact.list",
        update_method="crm.contact.update",
        get_method="crm.contact.get",
        title_fields=("FULL_NAME", "NAME", "LAST_NAME"),
        select_fields=("ID", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "leads": EntitySpec(
        key="leads",
        title="Leads",
        list_method="crm.lead.list",
        update_method="crm.lead.update",
        get_method="crm.lead.get",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "STATUS_SEMANTIC_ID", "COMPANY_ID", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "deals": EntitySpec(
        key="deals",
        title="Deals",
        list_method="crm.deal.list",
        update_method="crm.deal.update",
        get_method="crm.deal.get",
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


def parse_csv_values(value: Any) -> List[str]:
    if value is None:
        return []
    raw = str(value).strip()
    if raw.lower() in ALL_VALUES:
        return []
    parts: List[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    if len(parts) == 1 and parts[0].lower() in ALL_VALUES:
        return []
    return parts


def add_multi_filter(params: List[Tuple[str, Any]], field: str, values: Sequence[str]) -> None:
    if not values:
        return
    if len(values) == 1:
        params.append((f"filter[{field}]", values[0]))
        return
    for value in values:
        params.append((f"filter[{field}][]", value))


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

    def _paged_list(self, method: str, params_base: List[Tuple[str, Any]], limit: int = 0) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start: Any = 0
        while True:
            params = list(params_base)
            if start:
                params.append(("start", start))
            result = self.call(method, params)
            if isinstance(result, dict) and "items" in result:
                items = result.get("items") or []
                next_start = result.get("next")
            else:
                items = result or []
                next_start = None
            for item in items:
                rows.append(dict(item))
                if limit and len(rows) >= limit:
                    return rows
            if not next_start:
                break
            start = next_start
        return rows

    def list_entities(
        self,
        spec: EntitySpec,
        source_user_id: int,
        *,
        include_closed_deals: bool = False,
        deal_category_id: str = "all",
        deal_stage_ids: Sequence[str] = (),
        lead_status_ids: Sequence[str] = (),
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        params: List[Tuple[str, Any]] = [
            ("order[ID]", "ASC"),
            ("filter[ASSIGNED_BY_ID]", str(source_user_id)),
        ]
        if spec.key == "deals":
            if deal_category_id and str(deal_category_id).lower() not in ALL_VALUES:
                params.append(("filter[CATEGORY_ID]", str(deal_category_id)))
            add_multi_filter(params, "STAGE_ID", deal_stage_ids)
        if spec.key == "leads":
            add_multi_filter(params, "STATUS_ID", lead_status_ids)
        for field in spec.select_fields:
            params.append(("select[]", field))
        rows = self._paged_list(spec.list_method, params, limit=0)
        filtered: List[Dict[str, Any]] = []
        for row in rows:
            if spec.key == "deals" and not include_closed_deals and str(row.get("CLOSED", "")).upper() == "Y":
                continue
            filtered.append(row)
            if limit and len(filtered) >= limit:
                return filtered
        return filtered

    def related_deals_exist(
        self,
        *,
        source_user_id: int,
        company_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        deal_category_id: str = "all",
        deal_stage_ids: Sequence[str] = (),
        include_closed_deals: bool = False,
    ) -> bool:
        params: List[Tuple[str, Any]] = [
            ("order[ID]", "ASC"),
            ("filter[ASSIGNED_BY_ID]", str(source_user_id)),
        ]
        if company_id:
            params.append(("filter[COMPANY_ID]", str(company_id)))
        if contact_id:
            params.append(("filter[CONTACT_ID]", str(contact_id)))
        if deal_category_id and str(deal_category_id).lower() not in ALL_VALUES:
            params.append(("filter[CATEGORY_ID]", str(deal_category_id)))
        add_multi_filter(params, "STAGE_ID", deal_stage_ids)
        for field in ("ID", "STAGE_ID", "CLOSED", "CATEGORY_ID"):
            params.append(("select[]", field))
        rows = self._paged_list("crm.deal.list", params, limit=10)
        for row in rows:
            if not include_closed_deals and str(row.get("CLOSED", "")).upper() == "Y":
                continue
            return True
        return False

    def update_owner(self, spec: EntitySpec, entity_id: str, target_user_id: int) -> Any:
        params = [("id", str(entity_id)), ("fields[ASSIGNED_BY_ID]", str(target_user_id))]
        return self.call(spec.update_method, params)

    def get_entity(self, spec: EntitySpec, entity_id: Any) -> Dict[str, Any]:
        result = self.call(spec.get_method, [("id", str(entity_id))])
        return dict(result or {})

    def get_user_name(self, user_id: int) -> str:
        try:
            result = self.call("user.get", [("ID", str(user_id))])
            row = result[0] if isinstance(result, list) and result else result if isinstance(result, dict) else {}
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
        "entity_type", "entity_id", "entity_title", "source_user_id", "source_user_name",
        "target_user_id", "target_user_name", "action", "dry_run", "error",
        "filter_deal_stage_ids", "filter_lead_status_ids", "max_total", "company_id",
        "contact_id", "category_id", "stage_id", "status_id", "closed", "selection_mode", "crm_url_hint",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def should_skip_by_related_deal_stage(
    *,
    client: BitrixClient,
    spec: EntitySpec,
    item: Dict[str, Any],
    source_user_id: int,
    filter_company_contact_by_deal_stage: bool,
    deal_stage_ids: Sequence[str],
    deal_category_id: str,
    include_closed_deals: bool,
) -> Tuple[bool, str]:
    if not filter_company_contact_by_deal_stage or not deal_stage_ids:
        return False, ""
    if spec.key == "companies":
        company_id = str(item.get("ID") or "").strip()
        if not company_id:
            return True, "skip_no_company_id"
        ok = client.related_deals_exist(
            source_user_id=source_user_id, company_id=company_id,
            deal_category_id=deal_category_id, deal_stage_ids=deal_stage_ids,
            include_closed_deals=include_closed_deals,
        )
        return (not ok), "skip_no_related_deal_in_stage" if not ok else ""
    if spec.key == "contacts":
        contact_id = str(item.get("ID") or "").strip()
        if not contact_id:
            return True, "skip_no_contact_id"
        ok = client.related_deals_exist(
            source_user_id=source_user_id, contact_id=contact_id,
            deal_category_id=deal_category_id, deal_stage_ids=deal_stage_ids,
            include_closed_deals=include_closed_deals,
        )
        return (not ok), "skip_no_related_deal_in_stage" if not ok else ""
    return False, ""


def base_log_row(
    *,
    key: str,
    spec: EntitySpec,
    item: Dict[str, Any],
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    dry_run: bool,
    deal_stage_ids: Sequence[str],
    lead_status_ids: Sequence[str],
    max_total: int,
    portal_base_url: str,
    selection_mode: str,
) -> Dict[str, Any]:
    entity_id = str(item.get("ID") or "")
    row = {
        "entity_type": key,
        "entity_id": entity_id,
        "entity_title": entity_title(spec, item),
        "source_user_id": source_user_id,
        "source_user_name": source_user_name,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": dry_run,
        "filter_deal_stage_ids": ",".join(deal_stage_ids),
        "filter_lead_status_ids": ",".join(lead_status_ids),
        "max_total": max_total,
        "company_id": item.get("COMPANY_ID", ""),
        "contact_id": item.get("CONTACT_ID", ""),
        "category_id": item.get("CATEGORY_ID", ""),
        "stage_id": item.get("STAGE_ID", ""),
        "status_id": item.get("STATUS_ID", ""),
        "closed": item.get("CLOSED", ""),
        "crm_url_hint": crm_url_hint(key, entity_id, portal_base_url),
    }
    row["selection_mode"] = selection_mode
    return row


def update_or_log(
    *,
    client: BitrixClient,
    spec: EntitySpec,
    key: str,
    item: Dict[str, Any],
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    dry_run: bool,
    deal_stage_ids: Sequence[str],
    lead_status_ids: Sequence[str],
    max_total: int,
    portal_base_url: str,
    selection_mode: str,
    rows_out: List[Dict[str, Any]],
    require_source_owner: bool = True,
) -> Tuple[int, int]:
    """Return (updated_count, error_count)."""
    entity_id = str(item.get("ID") or "")
    base = base_log_row(
        key=key, spec=spec, item=item,
        source_user_id=source_user_id, source_user_name=source_user_name,
        target_user_id=target_user_id, target_user_name=target_user_name,
        dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
        max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
    )
    if not entity_id:
        rows_out.append({**base, "action": "skip_no_id", "error": "Entity ID is empty"})
        return 0, 1
    owner = str(item.get("ASSIGNED_BY_ID") or "").strip()
    if owner == str(target_user_id):
        rows_out.append({**base, "action": "skip_already_target_owner", "error": ""})
        return 0, 0
    if require_source_owner and owner != str(source_user_id):
        rows_out.append({**base, "action": "skip_not_source_owner", "error": f"ASSIGNED_BY_ID={owner}"})
        return 0, 0
    if dry_run:
        rows_out.append({**base, "action": "dry_run_update", "error": ""})
        return 0, 0
    try:
        client.update_owner(spec, entity_id, target_user_id)
        rows_out.append({**base, "action": "updated", "error": ""})
        return 1, 0
    except Exception as exc:
        rows_out.append({**base, "action": "update_error", "error": str(exc)})
        return 0, 1


def run_deal_package_mode(
    *,
    args: argparse.Namespace,
    client: BitrixClient,
    source_user_id: int,
    target_user_id: int,
    source_user_name: str,
    target_user_name: str,
    dry_run: bool,
    include_closed_deals: bool,
    deal_stage_ids: Sequence[str],
    lead_status_ids: Sequence[str],
    per_entity_limit: int,
    max_total: int,
    portal_base_url: str,
    rows_out: List[Dict[str, Any]],
) -> Tuple[int, int, int, int]:
    """Deal-first reassignment.

    When include_deals=true, max_total means max selected deals/applications, not max CRM entities.
    Related companies and contacts are updated after the selected deals and do not consume the deal limit.
    """
    selection_mode = "deal_package"
    deal_spec = ENTITY_SPECS["deals"]
    deal_limit = max_total or per_entity_limit or 0
    deals = client.list_entities(
        deal_spec,
        source_user_id,
        include_closed_deals=include_closed_deals,
        deal_category_id=str(args.deal_category_id or "all"),
        deal_stage_ids=deal_stage_ids,
        lead_status_ids=lead_status_ids,
        limit=deal_limit,
    )

    total_found = len(deals)
    total_selected = len(deals)
    total_updated = 0
    total_errors = 0
    company_ids: List[str] = []
    contact_ids: List[str] = []

    include_companies = parse_bool(args.include_companies, default=True)
    include_contacts = parse_bool(args.include_contacts, default=True)
    include_leads = parse_bool(args.include_leads, default=False)

    for deal in deals:
        cid = str(deal.get("COMPANY_ID") or "").strip()
        tid = str(deal.get("CONTACT_ID") or "").strip()
        if cid and cid not in company_ids:
            company_ids.append(cid)
        if tid and tid not in contact_ids:
            contact_ids.append(tid)
        upd, err = update_or_log(
            client=client, spec=deal_spec, key="deals", item=deal,
            source_user_id=source_user_id, source_user_name=source_user_name,
            target_user_id=target_user_id, target_user_name=target_user_name,
            dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
            max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
            rows_out=rows_out, require_source_owner=True,
        )
        total_updated += upd
        total_errors += err

    if include_companies:
        spec = ENTITY_SPECS["companies"]
        for company_id in company_ids:
            try:
                item = client.get_entity(spec, company_id)
                total_found += 1
                upd, err = update_or_log(
                    client=client, spec=spec, key="companies", item=item,
                    source_user_id=source_user_id, source_user_name=source_user_name,
                    target_user_id=target_user_id, target_user_name=target_user_name,
                    dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
                    max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
                    rows_out=rows_out, require_source_owner=True,
                )
                total_updated += upd
                total_errors += err
            except Exception as exc:
                total_errors += 1
                rows_out.append({
                    "entity_type": "companies", "entity_id": company_id, "entity_title": "",
                    "source_user_id": source_user_id, "source_user_name": source_user_name,
                    "target_user_id": target_user_id, "target_user_name": target_user_name,
                    "action": "get_error", "dry_run": dry_run, "error": str(exc),
                    "filter_deal_stage_ids": ",".join(deal_stage_ids),
                    "filter_lead_status_ids": ",".join(lead_status_ids),
                    "max_total": max_total, "selection_mode": selection_mode,
                    "crm_url_hint": crm_url_hint("companies", company_id, portal_base_url),
                })

    if include_contacts:
        spec = ENTITY_SPECS["contacts"]
        for contact_id in contact_ids:
            try:
                item = client.get_entity(spec, contact_id)
                total_found += 1
                upd, err = update_or_log(
                    client=client, spec=spec, key="contacts", item=item,
                    source_user_id=source_user_id, source_user_name=source_user_name,
                    target_user_id=target_user_id, target_user_name=target_user_name,
                    dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
                    max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
                    rows_out=rows_out, require_source_owner=True,
                )
                total_updated += upd
                total_errors += err
            except Exception as exc:
                total_errors += 1
                rows_out.append({
                    "entity_type": "contacts", "entity_id": contact_id, "entity_title": "",
                    "source_user_id": source_user_id, "source_user_name": source_user_name,
                    "target_user_id": target_user_id, "target_user_name": target_user_name,
                    "action": "get_error", "dry_run": dry_run, "error": str(exc),
                    "filter_deal_stage_ids": ",".join(deal_stage_ids),
                    "filter_lead_status_ids": ",".join(lead_status_ids),
                    "max_total": max_total, "selection_mode": selection_mode,
                    "crm_url_hint": crm_url_hint("contacts", contact_id, portal_base_url),
                })

    if include_leads:
        spec = ENTITY_SPECS["leads"]
        leads = client.list_entities(
            spec, source_user_id,
            include_closed_deals=include_closed_deals,
            deal_category_id=str(args.deal_category_id or "all"),
            deal_stage_ids=deal_stage_ids,
            lead_status_ids=lead_status_ids,
            limit=per_entity_limit,
        )
        for lead in leads:
            total_found += 1
            total_selected += 1
            upd, err = update_or_log(
                client=client, spec=spec, key="leads", item=lead,
                source_user_id=source_user_id, source_user_name=source_user_name,
                target_user_id=target_user_id, target_user_name=target_user_name,
                dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
                max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
                rows_out=rows_out, require_source_owner=True,
            )
            total_updated += upd
            total_errors += err

    return total_found, total_selected, total_updated, total_errors


def run(args: argparse.Namespace) -> int:
    dry_run = parse_bool(args.dry_run, default=True)
    source_user_id = parse_int(args.source_user_id, "source_user_id")
    target_user_id = parse_int(args.target_user_id, "target_user_id")
    if not source_user_id or not target_user_id:
        raise ValueError("source_user_id and target_user_id are required")
    if source_user_id == target_user_id:
        raise ValueError("source_user_id and target_user_id must be different")

    per_entity_limit = parse_int(args.limit, "limit", default=0) or 0
    max_total = parse_int(args.max_total, "max_total", default=0) or 0
    include_closed_deals = parse_bool(args.include_closed_deals, default=False)
    filter_company_contact_by_deal_stage = parse_bool(args.filter_company_contact_by_deal_stage, default=False)
    deal_stage_ids = parse_csv_values(args.deal_stage_ids)
    lead_status_ids = parse_csv_values(args.lead_status_ids)
    include_deals = parse_bool(args.include_deals, default=False)

    portal_base_url = args.portal_base_url or os.getenv("BITRIX_PORTAL_URL", "")
    timeout = parse_int(os.getenv("REQUEST_TIMEOUT", "60"), "REQUEST_TIMEOUT", default=60) or 60
    client = BitrixClient(os.environ.get("BITRIX_WEBHOOK_URL", ""), timeout=timeout)

    source_user_name = client.get_user_name(source_user_id)
    target_user_name = client.get_user_name(target_user_id)

    rows_out: List[Dict[str, Any]] = []

    if include_deals:
        total_found, total_selected, total_updated, total_errors = run_deal_package_mode(
            args=args, client=client,
            source_user_id=source_user_id, target_user_id=target_user_id,
            source_user_name=source_user_name, target_user_name=target_user_name,
            dry_run=dry_run, include_closed_deals=include_closed_deals,
            deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
            per_entity_limit=per_entity_limit, max_total=max_total,
            portal_base_url=portal_base_url, rows_out=rows_out,
        )
        selection_mode = "deal_package"
    else:
        selection_mode = "legacy_entity_order"
        total_found = 0
        total_selected = 0
        total_updated = 0
        total_errors = 0

        for key in selected_entities(args):
            if max_total and total_selected >= max_total:
                break
            spec = ENTITY_SPECS[key]
            remaining = (max_total - total_selected) if max_total else 0
            effective_limit = per_entity_limit
            if remaining and (not effective_limit or remaining < effective_limit):
                effective_limit = remaining
            fetch_limit = 0 if (spec.key in {"companies", "contacts"} and filter_company_contact_by_deal_stage and deal_stage_ids) else effective_limit
            try:
                items = client.list_entities(
                    spec,
                    source_user_id,
                    include_closed_deals=include_closed_deals,
                    deal_category_id=str(args.deal_category_id or "all"),
                    deal_stage_ids=deal_stage_ids,
                    lead_status_ids=lead_status_ids,
                    limit=fetch_limit,
                )
            except Exception as exc:
                total_errors += 1
                rows_out.append({
                    "entity_type": key, "entity_id": "", "entity_title": "",
                    "source_user_id": source_user_id, "source_user_name": source_user_name,
                    "target_user_id": target_user_id, "target_user_name": target_user_name,
                    "action": "list_error", "dry_run": dry_run, "error": str(exc),
                    "filter_deal_stage_ids": ",".join(deal_stage_ids),
                    "filter_lead_status_ids": ",".join(lead_status_ids), "max_total": max_total,
                    "selection_mode": selection_mode,
                })
                continue

            for item in items:
                if max_total and total_selected >= max_total:
                    break
                total_found += 1
                skip, reason = should_skip_by_related_deal_stage(
                    client=client, spec=spec, item=item, source_user_id=source_user_id,
                    filter_company_contact_by_deal_stage=filter_company_contact_by_deal_stage,
                    deal_stage_ids=deal_stage_ids, deal_category_id=str(args.deal_category_id or "all"),
                    include_closed_deals=include_closed_deals,
                )
                if skip:
                    base = base_log_row(
                        key=key, spec=spec, item=item,
                        source_user_id=source_user_id, source_user_name=source_user_name,
                        target_user_id=target_user_id, target_user_name=target_user_name,
                        dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
                        max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
                    )
                    rows_out.append({**base, "action": reason, "error": ""})
                    continue
                total_selected += 1
                upd, err = update_or_log(
                    client=client, spec=spec, key=key, item=item,
                    source_user_id=source_user_id, source_user_name=source_user_name,
                    target_user_id=target_user_id, target_user_name=target_user_name,
                    dry_run=dry_run, deal_stage_ids=deal_stage_ids, lead_status_ids=lead_status_ids,
                    max_total=max_total, portal_base_url=portal_base_url, selection_mode=selection_mode,
                    rows_out=rows_out, require_source_owner=True,
                )
                total_updated += upd
                total_errors += err

    out_path = Path(args.out)
    write_csv(out_path, rows_out)

    print("REASSIGN_CRM_OWNER_DONE")
    print(f"source_user_id={source_user_id} source_user_name={source_user_name}")
    print(f"target_user_id={target_user_id} target_user_name={target_user_name}")
    print(f"dry_run={dry_run}")
    print(f"entities={','.join(selected_entities(args))}")
    print(f"selection_mode={selection_mode}")
    print(f"deal_stage_ids={','.join(deal_stage_ids) or 'all'}")
    print(f"lead_status_ids={','.join(lead_status_ids) or 'all'}")
    print(f"filter_company_contact_by_deal_stage={filter_company_contact_by_deal_stage}")
    print(f"limit_per_entity={per_entity_limit}")
    print(f"max_total={max_total}")
    if include_deals:
        print("max_total_meaning=selected_deals")
    else:
        print("max_total_meaning=selected_entities")
    print(f"found={total_found}")
    print(f"selected={total_selected}")
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
    parser.add_argument("--deal-stage-ids", default="all", help="Comma-separated deal STAGE_ID values; all = no deal-stage filter")
    parser.add_argument("--lead-status-ids", default="all", help="Comma-separated lead STATUS_ID values; all = no lead-status filter")
    parser.add_argument("--filter-company-contact-by-deal-stage", default="false", help="For companies/contacts, update only if they have related source-user deal in selected deal stages")
    parser.add_argument("--limit", default="0", help="Max rows per entity type; 0 = no per-type limit")
    parser.add_argument("--max-total", default="0", help="Max total rows to update across all selected entity types; 0 = unlimited")
    parser.add_argument("--portal-base-url", default="https://b24-izmquv.bitrix24.kz")
    parser.add_argument("--out", default="exports/reassign_crm_owner_log.csv")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
