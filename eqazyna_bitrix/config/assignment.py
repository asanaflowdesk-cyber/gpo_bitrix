from __future__ import annotations

from typing import Any


DEFAULT_ASSIGNMENT_LOAD_STAGE_IDS = "ALL"


def parse_stage_ids(raw: Any) -> set[str]:
    """Parse comma-separated Bitrix STAGE_ID values for assignment-load counting.

    The 30-deal capacity is stage-based. By default ALL counts every
    non-closed deal. Use concrete STAGE_ID values when the limit must be
    narrower than all active work.
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

    By default the assignment load counts every non-closed e-Qazyna deal.
    This matches the operational meaning of "active applications": New, In work,
    agreement, document collection and any other non-final stage all consume a
    manager's attention.

    If a narrower limit is needed, pass concrete STAGE_ID values such as
    NEW,EXECUTING. Values ALL, OPEN or * mean every non-closed deal.
    """
    if is_closed_deal_record(deal):
        return False
    normalized = {str(value or "").strip().upper() for value in allowed_stage_ids}
    if not normalized or normalized.intersection({"ALL", "OPEN", "*"}):
        return True
    return stage_id_matches(deal.get("STAGE_ID"), allowed_stage_ids)
