from __future__ import annotations

from typing import Any


DEFAULT_ASSIGNMENT_LOAD_STAGE_IDS = "NEW,EXECUTING"


def parse_stage_ids(raw: Any) -> set[str]:
    """Parse comma-separated Bitrix STAGE_ID values for assignment-load counting.

    The 30-deal capacity is stage-based. By default only NEW and EXECUTING
    consume the limit, matching the business rule "Новая + В работе".
    """
    if raw is None:
        raw = DEFAULT_ASSIGNMENT_LOAD_STAGE_IDS
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = str(raw).split(",")
    return {str(part).strip().upper() for part in parts if str(part).strip()}


def stage_id_matches(stage_id: Any, allowed_stage_ids: set[str]) -> bool:
    """Return True when a Bitrix STAGE_ID matches configured stages.

    A config value NEW matches both NEW and category-prefixed C2:NEW.
    A config value C2:NEW matches only that exact category stage.
    """
    allowed = {value.strip().upper() for value in allowed_stage_ids if value and value.strip()}
    if not allowed:
        return False
    if "*" in allowed or "ALL" in allowed:
        return True
    normalized = str(stage_id or "").strip().upper()
    if not normalized:
        return False
    if normalized in allowed:
        return True
    if ":" in normalized and normalized.split(":", 1)[1] in allowed:
        return True
    return False


def is_closed_deal_record(deal: dict[str, Any]) -> bool:
    return str(deal.get("CLOSED") or "").strip().upper() == "Y"


def is_assignment_load_deal(deal: dict[str, Any], allowed_stage_ids: set[str]) -> bool:
    """Return True when a deal consumes manager capacity.

    By default the assignment load counts only the configured working stages
    NEW and EXECUTING. Agreement, document collection, failed and any other
    stages outside the configured list do not consume the 30-deal limit.

    Values ALL, OPEN or * are still supported for emergency broad counting,
    but they are not the production default.
    """
    if is_closed_deal_record(deal):
        return False
    normalized = {str(value or "").strip().upper() for value in allowed_stage_ids}
    if not normalized or normalized.intersection({"ALL", "OPEN", "*"}):
        return True
    return stage_id_matches(deal.get("STAGE_ID"), allowed_stage_ids)
