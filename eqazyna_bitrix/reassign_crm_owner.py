"""Manual Bitrix CRM package reassignment.

Main production mode:
- source_user_id accepts one ID or comma-separated IDs;
- target_user_id accepts one ID for all sources, or the same number of comma-separated IDs;
- moves the CRM package: deals + related companies/contacts + source-owned standalone companies/contacts;
- deal title gets a leading ❗ marker without duplication;
- deal stage is reset to NEW / C{CATEGORY_ID}:NEW;
- lost/failure reason field is cleared;
- timeline comments are added to updated CRM cards;
- dry-run writes the same CSV plan without changing Bitrix.
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off"}
NONE_VALUES = {"", "none", "нет", "no", "0", "-"}
ALL_VALUES = {"", "all", "*", "все", "любой", "любая"}
DEFAULT_FAILURE_REASON_FIELD = "UF_CRM_1779448756033"
DEAL_MARKER = "❗"

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


@dataclass(frozen=True)
class EntitySpec:
    key: str
    bitrix_type: str
    title: str
    list_method: str
    update_method: str
    get_method: str
    title_fields: Tuple[str, ...]
    select_fields: Tuple[str, ...]


ENTITY_SPECS: Dict[str, EntitySpec] = {
    "deals": EntitySpec(
        key="deals",
        bitrix_type="deal",
        title="Deals",
        list_method="crm.deal.list",
        update_method="crm.deal.update",
        get_method="crm.deal.get",
        title_fields=("TITLE",),
        select_fields=(
            "ID", "TITLE", "ASSIGNED_BY_ID", "CATEGORY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID",
            "CLOSED", "COMPANY_ID", "CONTACT_ID", "ORIGINATOR_ID", "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY",
            DEFAULT_FAILURE_REASON_FIELD,
        ),
    ),
    "companies": EntitySpec(
        key="companies",
        bitrix_type="company",
        title="Companies",
        list_method="crm.company.list",
        update_method="crm.company.update",
        get_method="crm.company.get",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "contacts": EntitySpec(
        key="contacts",
        bitrix_type="contact",
        title="Contacts",
        list_method="crm.contact.list",
        update_method="crm.contact.update",
        get_method="crm.contact.get",
        title_fields=("FULL_NAME", "NAME", "LAST_NAME"),
        select_fields=("ID", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
    "leads": EntitySpec(
        key="leads",
        bitrix_type="lead",
        title="Leads",
        list_method="crm.lead.list",
        update_method="crm.lead.update",
        get_method="crm.lead.get",
        title_fields=("TITLE",),
        select_fields=("ID", "TITLE", "ASSIGNED_BY_ID", "STATUS_ID", "COMPANY_ID", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY"),
    ),
}


class BitrixError(RuntimeError):
    pass


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
    if s == "":
        return default
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


def parse_id_list(value: Any, field_name: str) -> List[int]:
    values = parse_csv_values(value)
    if not values:
        raise ValueError(f"{field_name} is empty")
    ids: List[int] = []
    for raw in values:
        try:
            parsed = int(str(raw).strip())
        except Exception as exc:
            raise ValueError(f"Invalid {field_name} value: {raw!r}") from exc
        if parsed <= 0:
            raise ValueError(f"Invalid {field_name} value: {raw!r}")
        ids.append(parsed)
    return ids


def build_reassign_pairs(source_user_id: Any, target_user_id: Any) -> List[Tuple[int, int]]:
    sources = parse_id_list(source_user_id, "source_user_id")
    targets = parse_id_list(target_user_id, "target_user_id")

    if len(targets) == 1:
        return [(source, targets[0]) for source in sources]

    if len(sources) != len(targets):
        raise ValueError(
            "source_user_id and target_user_id must have the same number of IDs, "
            "or target_user_id must contain one ID for all sources"
        )

    return list(zip(sources, targets))


def parse_close_reason_id(value: Any) -> str:
    """Return normalized Bitrix close-reason enum ID or an empty string."""
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.lower() in NONE_VALUES:
        return ""
    reason_id = raw.split("-", 1)[0].strip()
    if reason_id not in CLOSE_REASON_OPTIONS:
        allowed = ", ".join(f"{k}={v}" for k, v in CLOSE_REASON_OPTIONS.items())
        raise ValueError(f"Invalid failure_reason_id={value!r}. Allowed: {allowed}")
    return reason_id


def close_reason_name(reason_id: str) -> str:
    return CLOSE_REASON_OPTIONS.get(str(reason_id).strip(), "")


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def entity_title(spec: EntitySpec, item: Dict[str, Any]) -> str:
    for field in spec.title_fields:
        value = item.get(field)
        if value:
            return str(value).strip()
    if spec.key == "contacts":
        return " ".join(str(item.get(k) or "").strip() for k in ("LAST_NAME", "NAME", "SECOND_NAME") if item.get(k)).strip()
    return ""


def target_stage_for_deal(deal: Dict[str, Any]) -> str:
    category_id = str(deal.get("CATEGORY_ID") or "0").strip()
    if category_id in {"", "0"}:
        return "NEW"
    return f"C{category_id}:NEW"


def marked_deal_title(title: str) -> str:
    title = str(title or "").strip()
    if title.startswith(DEAL_MARKER):
        return title
    return f"{DEAL_MARKER} {title}" if title else DEAL_MARKER


def crm_url_hint(entity_type: str, entity_id: str, portal_base_url: str) -> str:
    portal = (portal_base_url or "").rstrip("/")
    if not portal or not entity_id:
        return ""
    mapping = {
        "deals": "deal",
        "companies": "company",
        "contacts": "contact",
        "leads": "lead",
    }
    part = mapping.get(entity_type, entity_type)
    return f"{portal}/crm/{part}/details/{entity_id}/"


class BitrixClient:
    def __init__(self, webhook_url: str, timeout: int = 30, sleep_seconds: float = 0.05) -> None:
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL is empty")
        self.base_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()

    def call_full(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Dict[str, Any]:
        url = self.base_url + method + ".json"
        response = self.session.post(url, data=list(params or []), timeout=self.timeout)
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        try:
            payload = response.json()
        except Exception as exc:
            response.raise_for_status()
            raise BitrixError(f"Bitrix returned non-JSON response for {method}: {response.text[:500]}") from exc
        if response.status_code >= 400 or "error" in payload:
            raise BitrixError(f"Bitrix API error in {method}: status={response.status_code}, payload={payload}")
        return payload

    def call(self, method: str, params: Optional[Sequence[Tuple[str, Any]]] = None) -> Any:
        return self.call_full(method, params).get("result")

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
                rows.append(dict(item))
                if limit and len(rows) >= limit:
                    return rows
            if next_start in (None, "", False):
                break
            start = next_start
        return rows

    def list_by_owner(self, spec: EntitySpec, owner_id: int, *, limit: int = 0) -> List[Dict[str, Any]]:
        params: List[Tuple[str, Any]] = [
            ("order[ID]", "ASC"),
            ("filter[ASSIGNED_BY_ID]", str(owner_id)),
        ]
        for field in spec.select_fields:
            params.append(("select[]", field))
        return self._paged_list(spec.list_method, params, limit=limit)

    def get_entity(self, spec: EntitySpec, entity_id: str) -> Dict[str, Any]:
        result = self.call(spec.get_method, [("id", str(entity_id))])
        return dict(result or {})

    def update_entity(self, spec: EntitySpec, entity_id: str, fields: Dict[str, Any]) -> Any:
        params: List[Tuple[str, Any]] = [("id", str(entity_id))]
        for key, value in fields.items():
            params.append((f"fields[{key}]", value))
        params.append(("params[REGISTER_SONET_EVENT]", "Y"))
        return self.call(spec.update_method, params)

    def add_timeline_comment(self, spec: EntitySpec, entity_id: str, comment: str) -> str:
        result = self.call(
            "crm.timeline.comment.add",
            [
                ("fields[ENTITY_ID]", str(entity_id)),
                ("fields[ENTITY_TYPE]", spec.bitrix_type),
                ("fields[COMMENT]", comment),
            ],
        )
        return str(result or "")

    def get_user_name(self, user_id: int) -> str:
        try:
            result = self.call("user.get", [("ID", str(user_id))])
            if isinstance(result, list) and result:
                user = result[0]
                parts = [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")]
                name = " ".join(str(part).strip() for part in parts if part).strip()
                return name or str(user.get("LOGIN") or user_id)
        except Exception:
            pass
        return str(user_id)


def collect_package_for_source(
    *,
    client: BitrixClient,
    source_user_id: int,
    include_leads: bool,
    max_entities: int,
) -> Dict[str, List[Dict[str, Any]]]:
    deal_spec = ENTITY_SPECS["deals"]
    company_spec = ENTITY_SPECS["companies"]
    contact_spec = ENTITY_SPECS["contacts"]
    lead_spec = ENTITY_SPECS["leads"]

    deals = client.list_by_owner(deal_spec, source_user_id, limit=0)
    source_companies = client.list_by_owner(company_spec, source_user_id, limit=0)
    source_contacts = client.list_by_owner(contact_spec, source_user_id, limit=0)
    leads = client.list_by_owner(lead_spec, source_user_id, limit=0) if include_leads else []

    company_ids = unique_keep_order([str(deal.get("COMPANY_ID") or "") for deal in deals])
    contact_ids = unique_keep_order([str(deal.get("CONTACT_ID") or "") for deal in deals])

    companies_by_id: Dict[str, Dict[str, Any]] = {str(row.get("ID")): row for row in source_companies if row.get("ID")}
    contacts_by_id: Dict[str, Dict[str, Any]] = {str(row.get("ID")): row for row in source_contacts if row.get("ID")}

    for company_id in company_ids:
        if company_id not in companies_by_id:
            try:
                companies_by_id[company_id] = client.get_entity(company_spec, company_id)
            except Exception as exc:
                companies_by_id[company_id] = {"ID": company_id, "_load_error": str(exc)}

    for contact_id in contact_ids:
        if contact_id not in contacts_by_id:
            try:
                contacts_by_id[contact_id] = client.get_entity(contact_spec, contact_id)
            except Exception as exc:
                contacts_by_id[contact_id] = {"ID": contact_id, "_load_error": str(exc)}

    package = {
        "deals": deals,
        "companies": list(companies_by_id.values()),
        "contacts": list(contacts_by_id.values()),
        "leads": leads,
    }

    total = sum(len(values) for values in package.values())
    if max_entities and total > max_entities:
        raise RuntimeError(
            f"Stop-limit exceeded for source_user_id={source_user_id}: selected entities={total}, max_entities={max_entities}"
        )

    return package


def build_comment(
    *,
    base_comment: str,
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    entity_type: str,
    entity_id: str,
    dry_run: bool,
    deal_stage_reset: str = "",
    deal_title_marked: bool = False,
    failure_reason_cleared: bool = False,
) -> str:
    lines = [
        "Служебная отметка о перераспределении.",
        "",
        base_comment.strip() or "Административное перераспределение пакета ответственному.",
        f"Ответственный изменён: {source_user_name} (ID {source_user_id}) → {target_user_name} (ID {target_user_id}).",
        f"CRM-объект: {entity_type} #{entity_id}.",
    ]
    if deal_stage_reset:
        lines.append(f"Стадия сделки сброшена в: {deal_stage_reset}.")
    if deal_title_marked:
        lines.append("В название сделки добавлена метка ❗.")
    if failure_reason_cleared:
        lines.append(f"Очищена причина проигрыша: {DEFAULT_FAILURE_REASON_FIELD}.")
    if dry_run:
        lines.append("Режим: dry-run, изменения не записаны.")
    else:
        lines.append("Режим: write, изменения записаны.")
    return "\n".join(lines)


def make_base_row(
    *,
    spec: EntitySpec,
    item: Dict[str, Any],
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    dry_run: bool,
    portal_base_url: str,
) -> Dict[str, Any]:
    entity_id = str(item.get("ID") or "")
    return {
        "entity_type": spec.key,
        "entity_id": entity_id,
        "entity_title_before": entity_title(spec, item),
        "entity_title_after": "",
        "source_user_id": source_user_id,
        "source_user_name": source_user_name,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "before_owner_id": str(item.get("ASSIGNED_BY_ID") or ""),
        "verified_owner_id": "",
        "owner_verified": "",
        "stage_before": str(item.get("STAGE_ID") or ""),
        "stage_after": "",
        "stage_verified": "",
        "title_marked": "",
        "failure_reason_cleared": "",
        "timeline_comment_id": "",
        "timeline_comment_status": "",
        "action": "",
        "error": "",
        "company_id": str(item.get("COMPANY_ID") or ""),
        "contact_id": str(item.get("CONTACT_ID") or ""),
        "category_id": str(item.get("CATEGORY_ID") or ""),
        "closed": str(item.get("CLOSED") or ""),
        "originator_id": str(item.get("ORIGINATOR_ID") or ""),
        "origin_id": str(item.get("ORIGIN_ID") or ""),
        "crm_url_hint": crm_url_hint(spec.key, entity_id, portal_base_url),
    }


def update_one_entity(
    *,
    client: BitrixClient,
    spec: EntitySpec,
    item: Dict[str, Any],
    source_user_id: int,
    source_user_name: str,
    target_user_id: int,
    target_user_name: str,
    dry_run: bool,
    portal_base_url: str,
    base_comment: str,
    add_timeline_comment: bool,
    mark_deal_title: bool,
    reset_deal_stage: bool,
    clear_failure_reason: bool,
) -> Dict[str, Any]:
    row = make_base_row(
        spec=spec,
        item=item,
        source_user_id=source_user_id,
        source_user_name=source_user_name,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
        dry_run=dry_run,
        portal_base_url=portal_base_url,
    )

    entity_id = str(item.get("ID") or "").strip()
    if not entity_id:
        row.update({"action": "skip_no_id", "error": "Entity ID is empty"})
        return row

    if item.get("_load_error"):
        row.update({"action": "load_error", "error": str(item.get("_load_error"))})
        return row

    before_owner_id = str(item.get("ASSIGNED_BY_ID") or "").strip()
    if before_owner_id == str(target_user_id):
        row.update({"action": "skip_already_target_owner", "verified_owner_id": before_owner_id, "owner_verified": "true"})
        return row

    if before_owner_id and before_owner_id != str(source_user_id):
        row.update({"action": "skip_not_source_owner", "error": f"ASSIGNED_BY_ID={before_owner_id}"})
        return row

    fields: Dict[str, Any] = {"ASSIGNED_BY_ID": str(target_user_id)}
    comment_stage_reset = ""
    comment_title_marked = False
    comment_failure_cleared = False

    if spec.key == "deals":
        if mark_deal_title:
            new_title = marked_deal_title(str(item.get("TITLE") or ""))
            fields["TITLE"] = new_title
            row["entity_title_after"] = new_title
            row["title_marked"] = "true" if new_title != str(item.get("TITLE") or "") else "already_marked"
            comment_title_marked = True
        if reset_deal_stage:
            target_stage = target_stage_for_deal(item)
            fields["STAGE_ID"] = target_stage
            row["stage_after"] = target_stage
            comment_stage_reset = target_stage
        if clear_failure_reason:
            fields[DEFAULT_FAILURE_REASON_FIELD] = ""
            row["failure_reason_cleared"] = "true"
            comment_failure_cleared = True

    if dry_run:
        row["action"] = "dry_run_update"
        return row

    try:
        client.update_entity(spec, entity_id, fields)
        row["action"] = "updated"
    except Exception as exc:
        row.update({"action": "update_error", "error": str(exc)})
        return row

    try:
        verified = client.get_entity(spec, entity_id)
        verified_owner = str(verified.get("ASSIGNED_BY_ID") or "")
        row["verified_owner_id"] = verified_owner
        row["owner_verified"] = "true" if verified_owner == str(target_user_id) else "false"
        if spec.key == "deals":
            verified_stage = str(verified.get("STAGE_ID") or "")
            row["stage_verified"] = "true" if not reset_deal_stage or verified_stage == row["stage_after"] else "false"
            row["entity_title_after"] = str(verified.get("TITLE") or row["entity_title_after"] or "")
        if row.get("owner_verified") == "false" or row.get("stage_verified") == "false":
            row["action"] = "updated_but_verify_failed"
            row["error"] = f"verified_owner_id={verified_owner}; verified_stage={verified.get('STAGE_ID', '')}"
    except Exception as exc:
        row["action"] = "updated_but_verify_error"
        row["error"] = str(exc)

    if add_timeline_comment:
        comment = build_comment(
            base_comment=base_comment,
            source_user_id=source_user_id,
            source_user_name=source_user_name,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            entity_type=spec.key,
            entity_id=entity_id,
            dry_run=dry_run,
            deal_stage_reset=comment_stage_reset,
            deal_title_marked=comment_title_marked,
            failure_reason_cleared=comment_failure_cleared,
        )
        try:
            comment_id = client.add_timeline_comment(spec, entity_id, comment)
            row["timeline_comment_id"] = comment_id
            row["timeline_comment_status"] = "added" if comment_id else "empty_result"
        except Exception as exc:
            row["timeline_comment_status"] = "error"
            row["error"] = (str(row.get("error") or "") + f"; timeline_comment_error={exc}").strip("; ")
            if row["action"] == "updated":
                row["action"] = "updated_but_comment_error"

    return row


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type",
        "entity_id",
        "entity_title_before",
        "entity_title_after",
        "source_user_id",
        "source_user_name",
        "target_user_id",
        "target_user_name",
        "dry_run",
        "before_owner_id",
        "verified_owner_id",
        "owner_verified",
        "stage_before",
        "stage_after",
        "stage_verified",
        "title_marked",
        "failure_reason_cleared",
        "timeline_comment_id",
        "timeline_comment_status",
        "action",
        "error",
        "company_id",
        "contact_id",
        "category_id",
        "closed",
        "originator_id",
        "origin_id",
        "crm_url_hint",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def action_is_error(action: str) -> bool:
    return action in {
        "load_error",
        "update_error",
        "updated_but_verify_failed",
        "updated_but_verify_error",
        "updated_but_comment_error",
    }


def run(args: argparse.Namespace) -> int:
    pairs = build_reassign_pairs(args.source_user_id, args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    max_entities = parse_int(args.max_entities, "max_entities", default=5000) or 5000
    include_leads = parse_bool(args.include_leads, default=False)
    add_timeline_comment = parse_bool(args.add_timeline_comment, default=True)
    mark_deal_title = parse_bool(args.mark_deal_title, default=True)
    reset_deal_stage = parse_bool(args.reset_deal_stage, default=True)
    clear_failure_reason = parse_bool(args.clear_failure_reason, default=True)
    portal_base_url = str(args.portal_base_url or os.getenv("BITRIX_PORTAL_URL") or "")
    timeout = parse_int(os.getenv("REQUEST_TIMEOUT", "30"), "REQUEST_TIMEOUT", default=30) or 30

    client = BitrixClient(os.environ.get("BITRIX_WEBHOOK_URL", ""), timeout=timeout)
    user_name_cache: Dict[int, str] = {}

    def user_name(user_id: int) -> str:
        if user_id not in user_name_cache:
            user_name_cache[user_id] = client.get_user_name(user_id)
        return user_name_cache[user_id]

    rows_out: List[Dict[str, Any]] = []
    total_errors = 0
    total_updated = 0
    total_selected = 0

    for source_user_id, target_user_id in pairs:
        source_user_name = user_name(source_user_id)
        target_user_name = user_name(target_user_id)

        print(f"PAIR source={source_user_id} ({source_user_name}) -> target={target_user_id} ({target_user_name})")

        try:
            package = collect_package_for_source(
                client=client,
                source_user_id=source_user_id,
                include_leads=include_leads,
                max_entities=max_entities,
            )
        except Exception as exc:
            total_errors += 1
            rows_out.append({
                "entity_type": "package",
                "entity_id": "",
                "entity_title_before": "",
                "entity_title_after": "",
                "source_user_id": source_user_id,
                "source_user_name": source_user_name,
                "target_user_id": target_user_id,
                "target_user_name": target_user_name,
                "dry_run": str(dry_run).lower(),
                "action": "package_collect_error",
                "error": str(exc),
            })
            continue

        pair_selected = sum(len(items) for items in package.values())
        total_selected += pair_selected
        print(
            "PACKAGE_SELECTED "
            f"deals={len(package['deals'])} "
            f"companies={len(package['companies'])} "
            f"contacts={len(package['contacts'])} "
            f"leads={len(package['leads'])} "
            f"total={pair_selected}"
        )

        for key in ("deals", "companies", "contacts", "leads"):
            spec = ENTITY_SPECS[key]
            for item in package[key]:
                row = update_one_entity(
                    client=client,
                    spec=spec,
                    item=item,
                    source_user_id=source_user_id,
                    source_user_name=source_user_name,
                    target_user_id=target_user_id,
                    target_user_name=target_user_name,
                    dry_run=dry_run,
                    portal_base_url=portal_base_url,
                    base_comment=str(args.comment or ""),
                    add_timeline_comment=add_timeline_comment,
                    mark_deal_title=mark_deal_title,
                    reset_deal_stage=reset_deal_stage,
                    clear_failure_reason=clear_failure_reason,
                )
                rows_out.append(row)
                if row.get("action") == "updated":
                    total_updated += 1
                if action_is_error(str(row.get("action") or "")):
                    total_errors += 1

    out_path = Path(args.out)
    write_csv(out_path, rows_out)

    print("REASSIGN_CRM_OWNER_DONE")
    print(f"pairs={';'.join(f'{s}->{t}' for s, t in pairs)}")
    print(f"dry_run={dry_run}")
    print(f"max_entities={max_entities}")
    print(f"add_timeline_comment={add_timeline_comment}")
    print(f"mark_deal_title={mark_deal_title}")
    print(f"reset_deal_stage={reset_deal_stage}")
    print(f"clear_failure_reason={clear_failure_reason}")
    print(f"selected={total_selected}")
    print(f"updated={total_updated}")
    print(f"errors={total_errors}")
    print(f"out={out_path}")

    return 1 if total_errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reassign complete Bitrix CRM packages from one or more users to one or more target users")
    parser.add_argument("--source-user-id", required=True, help="One ID or comma-separated source user IDs")
    parser.add_argument("--target-user-id", required=True, help="One target ID for all sources, or comma-separated target IDs pairwise")
    parser.add_argument("--dry-run", default="true", help="true = no writes; false = write to Bitrix")
    parser.add_argument("--max-entities", default="5000", help="Stop-limit per source package")
    parser.add_argument("--comment", default="Административное перераспределение пакета ответственному.")
    parser.add_argument("--add-timeline-comment", default="true")
    parser.add_argument("--mark-deal-title", default="true")
    parser.add_argument("--reset-deal-stage", default="true")
    parser.add_argument("--clear-failure-reason", default="true")
    parser.add_argument("--include-leads", default="false")
    parser.add_argument("--portal-base-url", default="https://b24-izmquv.bitrix24.kz")
    parser.add_argument("--out", default="exports/reassign_crm_owner_log.csv")

    # Compatibility with older runs. These options are accepted but not used by the new package mode.
    parser.add_argument("--include-companies", default="true")
    parser.add_argument("--include-contacts", default="true")
    parser.add_argument("--include-deals", default="true")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--deal-category-id", default="all")
    parser.add_argument("--deal-stage-ids", default="all")
    parser.add_argument("--lead-status-ids", default="all")
    parser.add_argument("--filter-company-contact-by-deal-stage", default="false")
    parser.add_argument("--limit", default="0")
    parser.add_argument("--max-total", default="0")
    parser.add_argument("--target-deal-stage-id", default="")
    parser.add_argument("--failure-reason-field", default="")
    parser.add_argument("--failure-reason-id", default="")
    parser.add_argument("--failure-reason-text", default="")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
