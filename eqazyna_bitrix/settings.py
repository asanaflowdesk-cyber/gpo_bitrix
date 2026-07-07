from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class Settings:
    egov_api_key: str | None
    bitrix_webhook_url: str | None
    bitrix_deal_category_id: str
    bitrix_deal_stage_id: str
    bitrix_assigned_by_id: str | None
    bitrix_requisite_preset_id: str | None
    bitrix_requisite_bin_field: str
    request_timeout: int = 45
    eqazyna_request_timeout: int = 10
    bitrix_request_timeout: int = 30
    egov_request_timeout: int = 20
    polite_delay_seconds: float = 0.2
    bitrix_polite_delay_seconds: float = 0.05
    egov_polite_delay_seconds: float = 0.05
    max_consecutive_page_errors: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            egov_api_key=os.getenv("EGOV_API_KEY") or None,
            bitrix_webhook_url=os.getenv("BITRIX_WEBHOOK_URL") or None,
            bitrix_deal_category_id=os.getenv("BITRIX_DEAL_CATEGORY_ID", "0"),
            bitrix_deal_stage_id=os.getenv("BITRIX_DEAL_STAGE_ID", "NEW"),
            bitrix_assigned_by_id=os.getenv("BITRIX_ASSIGNED_BY_ID") or None,
            bitrix_requisite_preset_id=os.getenv("BITRIX_REQUISITE_PRESET_ID") or None,
            bitrix_requisite_bin_field=os.getenv("BITRIX_REQUISITE_BIN_FIELD", "RQ_BIN"),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "45")),
            eqazyna_request_timeout=int(os.getenv("EQAZYNA_REQUEST_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "10"))),
            bitrix_request_timeout=int(os.getenv("BITRIX_REQUEST_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "30"))),
            egov_request_timeout=int(os.getenv("EGOV_REQUEST_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "20"))),
            polite_delay_seconds=float(os.getenv("EQAZYNA_POLITE_DELAY_SECONDS", "0.2")),
            bitrix_polite_delay_seconds=float(os.getenv("BITRIX_POLITE_DELAY_SECONDS", "0.05")),
            egov_polite_delay_seconds=float(os.getenv("EGOV_POLITE_DELAY_SECONDS", "0.05")),
            max_consecutive_page_errors=int(os.getenv("EQAZYNA_MAX_CONSECUTIVE_PAGE_ERRORS", "1")),
        )
