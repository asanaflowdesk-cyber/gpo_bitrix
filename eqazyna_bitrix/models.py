from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass(slots=True)
class Application:
    created_at_raw: str
    doc_number: str
    bin: str
    applicant_name: str
    doc_type: str
    status: str
    source_url: str

    @property
    def application_key(self) -> str:
        return f"eQazyna|{self.doc_number}|{self.bin}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CompanyEnrichment:
    bin: str
    name: Optional[str] = None
    legal_address: Optional[str] = None
    director: Optional[str] = None
    activity: Optional[str] = None
    oked: Optional[str] = None
    registration_date: Optional[str] = None
    phone: Optional[str] = None
    match_name_score: Optional[int] = None
    match_oked_tpi: Optional[bool] = None
    match_reason: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProcessResult:
    app: Application
    enrichment: CompanyEnrichment
    action: str
    company_id: Optional[str] = None
    deal_id: Optional[str] = None
    lead_id: Optional[str] = None
    requisite_id: Optional[str] = None
    director_contact_id: Optional[str] = None
    director_contact_action: Optional[str] = None
    director_contact_error: Optional[str] = None
    assigned_by_id: Optional[int] = None
    assigned_by_name: Optional[str] = None
    assignment_reason: Optional[str] = None
    inherited_failed_stage_id: Optional[str] = None
    inherited_failed_reason: Optional[str] = None
    inherited_failed_from_deal_id: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at_raw": self.app.created_at_raw,
            "doc_number": self.app.doc_number,
            "bin": self.app.bin,
            "applicant_name": self.app.applicant_name,
            "doc_type": self.app.doc_type,
            "status": self.app.status,
            "application_key": self.app.application_key,
            "egov_name": self.enrichment.name,
            "legal_address": self.enrichment.legal_address,
            "director": self.enrichment.director,
            "activity": self.enrichment.activity,
            "oked": self.enrichment.oked,
            "registration_date": self.enrichment.registration_date,
            "phone": self.enrichment.phone,
            "egov_name_score": self.enrichment.match_name_score,
            "egov_oked_tpi": self.enrichment.match_oked_tpi,
            "egov_match_reason": self.enrichment.match_reason,
            "egov_error": self.enrichment.error,
            "egov_raw_preview": self._raw_preview(),
            "region": self.enrichment.region,
            "city": self.enrichment.city,
            "action": self.action,
            "company_id": self.company_id,
            "deal_id": self.deal_id,
            "lead_id": self.lead_id,
            "requisite_id": self.requisite_id,
            "director_contact_id": self.director_contact_id,
            "director_contact_action": self.director_contact_action,
            "director_contact_error": self.director_contact_error,
            "assigned_by_id": self.assigned_by_id,
            "assigned_by_name": self.assigned_by_name,
            "assignment_reason": self.assignment_reason,
            "inherited_failed_stage_id": self.inherited_failed_stage_id,
            "inherited_failed_reason": self.inherited_failed_reason,
            "inherited_failed_from_deal_id": self.inherited_failed_from_deal_id,
            "error": self.error,
            "source_url": self.app.source_url,
        }

    def _raw_preview(self) -> str | None:
        if not isinstance(self.enrichment.raw, dict) or not self.enrichment.raw:
            return None
        if self.enrichment.raw.get("raw_preview"):
            return str(self.enrichment.raw.get("raw_preview"))
        try:
            import json
            return json.dumps(self.enrichment.raw, ensure_ascii=False, default=str)[:3000]
        except Exception:
            return str(self.enrichment.raw)[:3000]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
