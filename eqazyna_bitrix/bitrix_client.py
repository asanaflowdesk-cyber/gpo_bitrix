from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests


class BitrixError(RuntimeError):
    pass


@dataclass(slots=True)
class BitrixClient:
    webhook_url: str
    timeout: int = 30
    polite_delay_seconds: float = 0.3
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if not self.webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL is empty")
        if self.session is None:
            self.session = requests.Session()
        self.webhook_url = self.webhook_url.rstrip("/") + "/"
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        payload = payload or {}
        url = self.webhook_url + method + ".json"
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
                try:
                    data = response.json()
                except Exception as exc:  # noqa: BLE001
                    raise BitrixError(f"Bitrix returned non-JSON response for {method}: {response.text[:500]}") from exc
                if response.status_code >= 400 or "error" in data:
                    raise BitrixError(f"{method}: {data.get('error')} {data.get('error_description')}".strip())
                time.sleep(self.polite_delay_seconds)
                return data.get("result")
            except (requests.Timeout, requests.ConnectionError, BitrixError) as exc:
                last_error = exc
                # Bitrix can throttle or briefly fail during large backfills. Retry transient errors,
                # but keep validation errors visible after a short retry cycle.
                if attempt < 3:
                    time.sleep(min(2 * attempt, 6))
                    continue
                break
        raise BitrixError(str(last_error) if last_error else f"{method}: unknown Bitrix error")

    # Companies
    def find_company_by_origin(self, bin_number: str) -> dict[str, Any] | None:
        result = self.call(
            "crm.company.list",
            {
                "order": {"ID": "DESC"},
                "filter": {"ORIGINATOR_ID": "EQAZYNA", "ORIGIN_ID": bin_number},
                "select": ["ID", "TITLE", "ORIGINATOR_ID", "ORIGIN_ID", "COMMENTS", "ASSIGNED_BY_ID"],
            },
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    def find_company_by_requisite_bin(self, bin_number: str, bin_field: str = "RQ_BIN") -> dict[str, Any] | None:
        """Fallback lookup for companies created manually or by old integration versions."""
        filters_to_try = [
            {"ENTITY_TYPE_ID": 4, bin_field: bin_number},
            {"ENTITY_TYPE_ID": 4, "RQ_BIN": bin_number},
            {"ENTITY_TYPE_ID": 4, "RQ_INN": bin_number},
        ]
        seen_filters: list[dict[str, Any]] = []
        for flt in filters_to_try:
            if flt in seen_filters:
                continue
            seen_filters.append(flt)
            try:
                result = self.call(
                    "crm.requisite.list",
                    {
                        "order": {"ID": "DESC"},
                        "filter": flt,
                        "select": ["ID", "ENTITY_TYPE_ID", "ENTITY_ID", "NAME", "RQ_COMPANY_NAME", "RQ_BIN", "RQ_INN"],
                    },
                )
            except BitrixError:
                continue
            if isinstance(result, list) and result:
                company_id = str(result[0].get("ENTITY_ID") or "")
                if company_id:
                    return self.get_company(company_id)
        return None

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        result = self.call("crm.company.get", {"id": int(company_id)})
        return result if isinstance(result, dict) else None

    def create_company(self, fields: dict[str, Any]) -> str:
        result = self.call("crm.company.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return str(result)

    def update_company(self, company_id: str, fields: dict[str, Any]) -> bool:
        self.call("crm.company.update", {"id": company_id, "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return True

    # Leads
    def find_lead_by_origin(self, origin_id: str, originator_id: str = "EQAZYNA_LEAD") -> dict[str, Any] | None:
        result = self.call(
            "crm.lead.list",
            {
                "order": {"ID": "DESC"},
                "filter": {"ORIGINATOR_ID": originator_id, "ORIGIN_ID": origin_id},
                "select": [
                    "ID",
                    "TITLE",
                    "STATUS_ID",
                    "ASSIGNED_BY_ID",
                    "COMPANY_TITLE",
                    "COMMENTS",
                    "ORIGINATOR_ID",
                    "ORIGIN_ID",
                ],
            },
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    def create_lead(self, fields: dict[str, Any]) -> str:
        result = self.call("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return str(result)

    def update_lead(self, lead_id: str, fields: dict[str, Any]) -> bool:
        self.call("crm.lead.update", {"id": int(lead_id), "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return True

    # Deals
    def find_deal_by_origin(self, deal_key: str) -> dict[str, Any] | None:
        result = self.call(
            "crm.deal.list",
            {
                "order": {"ID": "DESC"},
                "filter": {"ORIGINATOR_ID": "EQAZYNA", "ORIGIN_ID": deal_key},
                "select": ["ID", "TITLE", "COMPANY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "STAGE_ID", "ASSIGNED_BY_ID"],
            },
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    def create_deal(self, fields: dict[str, Any]) -> str:
        result = self.call("crm.deal.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return str(result)

    def update_deal(self, deal_id: str, fields: dict[str, Any]) -> bool:
        self.call("crm.deal.update", {"id": deal_id, "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        return True

    # Timeline
    def add_timeline_comment(self, entity_type: str, entity_id: str, comment: str) -> str | None:
        result = self.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": int(entity_id), "ENTITY_TYPE": entity_type, "COMMENT": comment}},
        )
        return str(result) if result is not None else None

    # Requisites
    def list_requisites_for_company(self, company_id: str) -> list[dict[str, Any]]:
        result = self.call(
            "crm.requisite.list",
            {
                "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": int(company_id)},
                "select": ["ID", "ENTITY_TYPE_ID", "ENTITY_ID", "PRESET_ID", "NAME", "RQ_COMPANY_NAME", "RQ_COMPANY_FULL_NAME", "RQ_BIN", "RQ_INN", "RQ_DIRECTOR"],
            },
        )
        return result if isinstance(result, list) else []

    def add_requisite(self, fields: dict[str, Any]) -> str:
        result = self.call("crm.requisite.add", {"fields": fields})
        return str(result)

    def update_requisite(self, requisite_id: str, fields: dict[str, Any]) -> bool:
        self.call("crm.requisite.update", {"id": int(requisite_id), "fields": fields})
        return True

    def requisite_presets(self) -> Any:
        return self.call("crm.requisite.preset.list", {"select": ["ID", "NAME", "COUNTRY_ID", "SORT", "ACTIVE"]})

# ---- Contact helpers injected by director-contact patch ----
def _bitrix_call_full(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    url = self.webhook_url + method + ".json"
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                raise BitrixError(f"Bitrix returned non-JSON response for {method}: {response.text[:500]}") from exc
            if response.status_code >= 400 or "error" in data:
                raise BitrixError(f"{method}: {data.get('error')} {data.get('error_description')}".strip())
            time.sleep(self.polite_delay_seconds)
            return data
        except (requests.Timeout, requests.ConnectionError, BitrixError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(min(2 * attempt, 6))
                continue
            break
    raise BitrixError(str(last_error) if last_error else f"{method}: unknown Bitrix error")


def _bitrix_list_all(self, method: str, payload: dict[str, Any] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    payload = dict(payload or {})
    items: list[dict[str, Any]] = []
    start: int | str | None = 0
    while start is not None:
        payload["start"] = start
        data = self.call_full(method, payload)
        result = data.get("result")
        if isinstance(result, list):
            items.extend(result)
        if limit and len(items) >= limit:
            return items[:limit]
        start = data.get("next")
    return items


def _find_contact_by_fio(self, last_name: str, name: str, second_name: str = "") -> dict[str, Any] | None:
    filters = {"LAST_NAME": last_name, "NAME": name}
    if second_name:
        filters["SECOND_NAME"] = second_name
    result = self.call(
        "crm.contact.list",
        {
            "order": {"ID": "DESC"},
            "filter": filters,
            "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME", "POST", "COMMENTS", "COMPANY_ID"],
        },
    )
    if isinstance(result, list) and result:
        return result[0]
    return None


def _create_contact(self, fields: dict[str, Any]) -> str:
    result = self.call("crm.contact.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
    return str(result)


def _update_contact(self, contact_id: str, fields: dict[str, Any]) -> bool:
    self.call("crm.contact.update", {"id": int(contact_id), "fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
    return True


def _company_contact_ids(self, company_id: str) -> set[str]:
    result = self.call("crm.company.contact.items.get", {"id": int(company_id)})
    if not isinstance(result, list):
        return set()
    return {str(item.get("CONTACT_ID")) for item in result if item.get("CONTACT_ID") is not None}


def _deal_contact_ids(self, deal_id: str) -> set[str]:
    result = self.call("crm.deal.contact.items.get", {"id": int(deal_id)})
    if not isinstance(result, list):
        return set()
    return {str(item.get("CONTACT_ID")) for item in result if item.get("CONTACT_ID") is not None}


def _link_contact_to_company(self, company_id: str, contact_id: str, primary: bool = True) -> bool:
    if str(contact_id) in self.company_contact_ids(company_id):
        return False
    self.call(
        "crm.company.contact.add",
        {"id": int(company_id), "fields": {"CONTACT_ID": int(contact_id), "IS_PRIMARY": "Y" if primary else "N", "SORT": 1000}},
    )
    return True


def _link_contact_to_deal(self, deal_id: str, contact_id: str, primary: bool = True) -> bool:
    if str(contact_id) in self.deal_contact_ids(deal_id):
        return False
    self.call(
        "crm.deal.contact.add",
        {"id": int(deal_id), "fields": {"CONTACT_ID": int(contact_id), "IS_PRIMARY": "Y" if primary else "N", "SORT": 1000}},
    )
    return True


def _list_eqazyna_companies(self, limit: int | None = None) -> list[dict[str, Any]]:
    return self.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": "EQAZYNA"},
            "select": ["ID", "TITLE", "COMMENTS", "ORIGINATOR_ID", "ORIGIN_ID", "ASSIGNED_BY_ID"],
        },
        limit=limit,
    )


def _list_eqazyna_deals(self, limit: int | None = None) -> list[dict[str, Any]]:
    return self.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": "EQAZYNA"},
            "select": [
                "ID",
                "TITLE",
                "COMPANY_ID",
                "CATEGORY_ID",
                "STAGE_ID",
                "STAGE_SEMANTIC_ID",
                "CLOSED",
                "CLOSEDATE",
                "DATE_MODIFY",
                "COMMENTS",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
                "ASSIGNED_BY_ID",
                "LOSE_REASON",
                "UF_*",
            ],
        },
        limit=limit,
    )


def _list_deals_by_company(self, company_id: str, only_eqazyna: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {"COMPANY_ID": int(company_id)}
    if only_eqazyna:
        flt["ORIGINATOR_ID"] = "EQAZYNA"
    return self.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": ["ID", "TITLE", "COMPANY_ID", "STAGE_ID", "CATEGORY_ID", "CLOSED", "COMMENTS", "ORIGINATOR_ID", "ORIGIN_ID", "ASSIGNED_BY_ID"],
        },
        limit=limit,
    )


def _list_eqazyna_leads(self, limit: int | None = None) -> list[dict[str, Any]]:
    return self.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": "EQAZYNA_LEAD"},
            "select": ["ID", "TITLE", "STATUS_ID", "COMPANY_TITLE", "ORIGINATOR_ID", "ORIGIN_ID", "ASSIGNED_BY_ID"],
        },
        limit=limit,
    )

# monkey-patch methods onto dataclass class without rewriting the whole file
BitrixClient.call_full = _bitrix_call_full  # type: ignore[attr-defined]
BitrixClient.list_all = _bitrix_list_all  # type: ignore[attr-defined]
BitrixClient.find_contact_by_fio = _find_contact_by_fio  # type: ignore[attr-defined]
BitrixClient.create_contact = _create_contact  # type: ignore[attr-defined]
BitrixClient.update_contact = _update_contact  # type: ignore[attr-defined]
BitrixClient.company_contact_ids = _company_contact_ids  # type: ignore[attr-defined]
BitrixClient.deal_contact_ids = _deal_contact_ids  # type: ignore[attr-defined]
BitrixClient.link_contact_to_company = _link_contact_to_company  # type: ignore[attr-defined]
BitrixClient.link_contact_to_deal = _link_contact_to_deal  # type: ignore[attr-defined]
BitrixClient.list_eqazyna_companies = _list_eqazyna_companies  # type: ignore[attr-defined]
BitrixClient.list_eqazyna_deals = _list_eqazyna_deals  # type: ignore[attr-defined]
BitrixClient.list_deals_by_company = _list_deals_by_company  # type: ignore[attr-defined]
BitrixClient.list_eqazyna_leads = _list_eqazyna_leads  # type: ignore[attr-defined]
