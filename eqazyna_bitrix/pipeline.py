from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Iterable
from collections import Counter
from datetime import datetime, timezone

from .bitrix_client import BitrixClient, BitrixError
from .formatter import build_company_summary, build_deal_comment, build_deal_title, build_lead_comment, build_lead_title
from .models import Application, CompanyEnrichment, ProcessResult
from .director import director_identity_key, director_identity_keys, director_keys_match, split_director_fio
from .config.assignment import DEFAULT_ASSIGNMENT_LOAD_STAGE_IDS, is_assignment_load_deal, parse_stage_ids, stage_id_matches
from .config.special_owner_pins import load_special_owner_pin_config
from .distribute_companies import (
    ALLOWED_USER_IDS,
    SOURCE_RESPONSIBLE_IDS,
    USER_NAMES,
    _extract_director_from_comments,
    _normalize_bin,
    _normalize_text,
)


_SPECIAL_OWNER_PIN_CONFIG = load_special_owner_pin_config()
SPECIAL_OWNER_BY_CONTACT_ID = _SPECIAL_OWNER_PIN_CONFIG.owner_by_contact_id


@dataclass(slots=True)
class BitrixPipelineConfig:
    crm_mode: str = "deal"  # deal = old company+deal flow, lead = new lead-only flow
    deal_category_id: str = "0"
    deal_stage_id: str = "NEW"
    lead_status_id: str = "NEW"
    assigned_by_id: str | None = None
    requisite_preset_id: str | None = None
    requisite_bin_field: str = "RQ_BIN"
    dry_run: bool = False
    assignment_limit_per_manager: int = 30
    assignment_load_stage_ids: str = DEFAULT_ASSIGNMENT_LOAD_STAGE_IDS
    inherit_failed_deals_by_director: bool = True
    failed_deal_stage_ids: str = "LOSE"
    failed_deal_reason_fields: str = "UF_CRM_1779448756033"


@dataclass(slots=True)
class FailedDealInheritance:
    # Target stage to write into the new deal. Keep this canonical (for example LOSE),
    # even when an old source deal was stored as C2:LOSE.
    stage_id: str
    source_stage_id: str | None = None
    reason: str | None = None
    reason_field: str | None = None
    source_deal_id: str | None = None
    source_deal_title: str | None = None


class BitrixPipeline:
    def __init__(self, client: BitrixClient, config: BitrixPipelineConfig) -> None:
        self.client = client
        self.config = config
        self._director_contact_cache: dict[str, str] = {}
        self._director_pin_lookup_cache: dict[str, tuple[int | None, str | None, str | None]] = {}
        self._eqazyna_companies_cache: list[dict[str, Any]] | None = None
        self._eqazyna_deals_cache: list[dict[str, Any]] | None = None
        self._failed_deal_by_director_cache: dict[str, FailedDealInheritance | None] = {}
        # Per-run assignment memory: one BIN / one director package must keep one manager
        # while a single parser call processes several e-Qazyna applications.
        self._assignment_cache: dict[str, tuple[int, str]] = {}
        self._projected_load_delta: Counter[int] = Counter()
        self._manager_load_cache: dict[int, int] | None = None

    def process(
        self,
        app: Application,
        enrichment: CompanyEnrichment,
        *,
        existing_deal_prechecked: bool = False,
    ) -> ProcessResult:
        if (self.config.crm_mode or "deal").lower() == "lead":
            return self.process_lead(app, enrichment)
        try:
            # Deal origin is the immutable application key. If the application was
            # already loaded earlier, the row is intentionally skipped: no company
            # update, no owner rewrite, no stage rewrite, no contact/requisite work.
            if not existing_deal_prechecked:
                existing_deal = self.client.find_deal_by_origin(app.application_key)
                if existing_deal:
                    return ProcessResult(
                        app,
                        enrichment,
                        action="existing_deal_skipped",
                        company_id=str(existing_deal.get("COMPANY_ID") or "") or None,
                        deal_id=str(existing_deal.get("ID") or "") or None,
                        assigned_by_id=self._record_assigned_by_id(existing_deal),
                        assigned_by_name=self._user_name(self._record_assigned_by_id(existing_deal)),
                        assignment_reason="existing_deal_no_update",
                    )

            company = self.client.find_company_by_origin(app.bin)
            if not company:
                company = self.client.find_company_by_requisite_bin(app.bin, self.config.requisite_bin_field)
            company_created = False
            target_responsible_id, assignment_reason = self._resolve_target_responsible(app, enrichment, company)

            if company:
                company_id = str(company["ID"])
                # Backfill origin fields for manually created companies / old integration records.
                origin_update = {}
                if str(company.get("ORIGINATOR_ID") or "") != "EQAZYNA":
                    origin_update["ORIGINATOR_ID"] = "EQAZYNA"
                if str(company.get("ORIGIN_ID") or "") != app.bin:
                    origin_update["ORIGIN_ID"] = app.bin
                if target_responsible_id and self._record_assigned_by_id(company) != target_responsible_id:
                    origin_update["ASSIGNED_BY_ID"] = target_responsible_id
                if origin_update and not self.config.dry_run:
                    self.client.update_company(company_id, origin_update)
            else:
                company_fields = self._company_fields(app, enrichment, responsible_id=target_responsible_id)
                if self.config.dry_run:
                    failed_inheritance = self._failed_deal_inheritance_for_director(enrichment.director)
                    self._remember_created_deal_load(target_responsible_id, failed_inheritance.stage_id if failed_inheritance else self.config.deal_stage_id)
                    return ProcessResult(
                        app,
                        enrichment,
                        action="dry_run_company_and_deal",
                        company_id="DRY_RUN",
                        deal_id="DRY_RUN",
                        assigned_by_id=target_responsible_id,
                        assigned_by_name=self._user_name(target_responsible_id),
                        assignment_reason=assignment_reason,
                        inherited_failed_stage_id=failed_inheritance.stage_id if failed_inheritance else None,
                        inherited_failed_reason=failed_inheritance.reason if failed_inheritance else None,
                        inherited_failed_from_deal_id=failed_inheritance.source_deal_id if failed_inheritance else None,
                    )
                company_id = self.client.create_company(company_fields)
                company_created = True

            requisite_id = None
            if not self.config.dry_run:
                requisite_id = self.ensure_requisite(company_id, app, enrichment)

            failed_inheritance = self._failed_deal_inheritance_for_director(enrichment.director)
            deal_fields = self._deal_fields(app, enrichment, company_id, responsible_id=target_responsible_id, failed_inheritance=failed_inheritance)
            if self.config.dry_run:
                self._remember_created_deal_load(target_responsible_id, failed_inheritance.stage_id if failed_inheritance else self.config.deal_stage_id)
                return ProcessResult(
                    app,
                    enrichment,
                    action="dry_run_create_deal",
                    company_id=company_id,
                    deal_id="DRY_RUN",
                    assigned_by_id=target_responsible_id,
                    assigned_by_name=self._user_name(target_responsible_id),
                    assignment_reason=assignment_reason,
                    inherited_failed_stage_id=failed_inheritance.stage_id if failed_inheritance else None,
                    inherited_failed_reason=failed_inheritance.reason if failed_inheritance else None,
                    inherited_failed_from_deal_id=failed_inheritance.source_deal_id if failed_inheritance else None,
                )

            deal_id = self.client.create_deal(deal_fields)
            self._remember_created_deal_load(target_responsible_id, failed_inheritance.stage_id if failed_inheritance else self.config.deal_stage_id)
            company_update = self._company_update_fields(app, enrichment)
            if target_responsible_id:
                company_update["ASSIGNED_BY_ID"] = target_responsible_id
            self.client.update_company(company_id, company_update)
            comment = build_deal_comment(app, enrichment)
            self.client.add_timeline_comment("deal", deal_id, comment)
            self.client.add_timeline_comment("company", company_id, f"Создана сделка по заявке e-Qazyna: {app.doc_number}\n\n{comment}")
            contact_id, contact_action, contact_error = self.ensure_director_contact(company_id, deal_id, enrichment, responsible_id=target_responsible_id)

            return ProcessResult(
                app,
                enrichment,
                action="created_company_created_deal" if company_created else "existing_company_created_deal",
                company_id=company_id,
                deal_id=deal_id,
                requisite_id=requisite_id,
                director_contact_id=contact_id,
                director_contact_action=contact_action,
                director_contact_error=contact_error,
                assigned_by_id=target_responsible_id,
                assigned_by_name=self._user_name(target_responsible_id),
                assignment_reason=assignment_reason,
                inherited_failed_stage_id=failed_inheritance.stage_id if failed_inheritance else None,
                inherited_failed_reason=failed_inheritance.reason if failed_inheritance else None,
                inherited_failed_from_deal_id=failed_inheritance.source_deal_id if failed_inheritance else None,
            )
        except Exception as exc:  # noqa: BLE001 - log per row instead of failing whole export
            return ProcessResult(app, enrichment, action="error", error=str(exc))


    def process_lead(self, app: Application, enrichment: CompanyEnrichment) -> ProcessResult:
        """Create/update one lead per BIN without creating clients or deals.

        New CRM model:
        - lead = primary cold-processing queue item;
        - company/contact/deal are not created by this flow;
        - multiple e-Qazyna applications for one BIN are appended into the lead comments.
        """
        try:
            origin_id = app.bin
            lead = self.client.find_lead_by_origin(origin_id)
            lead_created = False
            if lead:
                lead_id = str(lead["ID"])
                existing_comments = str(lead.get("COMMENTS") or "")
                fields = self._lead_update_fields(app, enrichment, existing_comments)
                if self.config.assigned_by_id:
                    fields["ASSIGNED_BY_ID"] = int(self.config.assigned_by_id)
                if self.config.dry_run:
                    return ProcessResult(app, enrichment, action="dry_run_update_lead", lead_id=lead_id)
                self.client.update_lead(lead_id, fields)
                action = "existing_lead_updated"
            else:
                fields = self._lead_fields(app, enrichment)
                if self.config.dry_run:
                    return ProcessResult(app, enrichment, action="dry_run_create_lead", lead_id="DRY_RUN")
                lead_id = self.client.create_lead(fields)
                lead_created = True
                action = "created_lead"

            # Add a timeline comment only when the application key is not already in comments.
            # This prevents duplicate spam if GitHub Action is run twice for the same page.
            if not self.config.dry_run and not lead_created:
                existing_comments = str((lead or {}).get("COMMENTS") or "")
                if app.application_key not in existing_comments:
                    self.client.add_timeline_comment("lead", lead_id, build_deal_comment(app, enrichment))

            return ProcessResult(app, enrichment, action=action, lead_id=lead_id)
        except Exception as exc:  # noqa: BLE001 - log per row instead of failing whole export
            return ProcessResult(app, enrichment, action="error", error=str(exc))

    def _lead_update_fields(self, app: Application, enr: CompanyEnrichment, existing_comments: str | None = None) -> dict[str, object]:
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enr),
            "COMPANY_TITLE": enr.name or app.applicant_name,
            "COMMENTS": build_lead_comment(app, enr, existing_comments),
        }
        phone = self._phone_multifield(enr)
        if phone:
            fields["PHONE"] = phone
        if self.config.lead_status_id:
            fields["STATUS_ID"] = self.config.lead_status_id
        return fields

    def _lead_fields(self, app: Application, enr: CompanyEnrichment) -> dict[str, object]:
        fields: dict[str, object] = {
            "TITLE": build_lead_title(app, enr),
            "COMPANY_TITLE": enr.name or app.applicant_name,
            "STATUS_ID": self.config.lead_status_id or "NEW",
            "OPENED": "Y",
            "COMMENTS": build_lead_comment(app, enr),
            "ORIGINATOR_ID": "EQAZYNA_LEAD",
            "ORIGIN_ID": app.bin,
            "SOURCE_ID": "OTHER",
            "SOURCE_DESCRIPTION": "e-Qazyna minerals registry",
        }
        phone = self._phone_multifield(enr)
        if phone:
            fields["PHONE"] = phone
        if self.config.assigned_by_id:
            fields["ASSIGNED_BY_ID"] = int(self.config.assigned_by_id)
        return fields

    def _configured_assigned_by_id(self) -> int | None:
        if not self.config.assigned_by_id:
            return None
        try:
            return int(self.config.assigned_by_id)
        except (TypeError, ValueError):
            return None

    def _record_assigned_by_id(self, record: dict | None) -> int | None:
        if not record:
            return None
        try:
            value = record.get("ASSIGNED_BY_ID")
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _company_owner_or_configured(self, company: dict | None) -> int | None:
        # Priority rule for the simplified CRM contour:
        # existing client/company owner wins over the fallback workflow user.
        # This prevents new/updated deals from falling back to the webhook owner
        # when a manager is already fixed on the company card.
        return self._record_assigned_by_id(company) or self._configured_assigned_by_id()

    def _user_name(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        return USER_NAMES.get(user_id, f"User {user_id}")

    def _source_responsible_ids(self) -> set[int]:
        return set(SOURCE_RESPONSIBLE_IDS)

    def _is_allowed_manager(self, user_id: int | None) -> bool:
        return user_id in ALLOWED_USER_IDS

    def _is_source_responsible(self, user_id: int | None) -> bool:
        return user_id in self._source_responsible_ids()

    def _assignment_cache_keys(self, bin_value: str | None, director: str | None) -> list[str]:
        keys: list[str] = []
        normalized_bin = _normalize_bin(bin_value or "")
        if normalized_bin:
            keys.append(f"bin|{normalized_bin}")
        for director_key in director_identity_keys(director):
            keys.append(f"director|{director_key}")
        return keys

    def _cached_assignment(self, bin_value: str | None, director: str | None) -> tuple[int | None, str | None]:
        for key in self._assignment_cache_keys(bin_value, director):
            cached = self._assignment_cache.get(key)
            if cached:
                return cached[0], cached[1]
        return None, None

    def _remember_assignment(self, bin_value: str | None, director: str | None, user_id: int | None, reason: str | None) -> None:
        """Remember one owner for every BIN/director key and reject split packages.

        The previous implementation used ``setdefault``. If a second BIN of the
        same director received another owner, the BIN key was stored for the new
        manager while the director key silently stayed on the old manager. That
        is exactly how one director could receive deals under different people.
        """
        if not user_id:
            return
        cache_reason = reason or "runtime_cached_package_owner"
        keys = self._assignment_cache_keys(bin_value, director)
        conflicts: list[str] = []
        for key in keys:
            existing = self._assignment_cache.get(key)
            if existing and existing[0] != user_id:
                conflicts.append(
                    f"{key}: {existing[0]} ({self._user_name(existing[0])}) != "
                    f"{user_id} ({self._user_name(user_id)})"
                )
        if conflicts:
            raise BitrixError(
                "DIRECTOR_PACKAGE_RUNTIME_CONFLICT: one BIN/director package cannot be assigned "
                "to different managers in one run. " + "; ".join(conflicts)
            )
        for key in keys:
            self._assignment_cache[key] = (user_id, cache_reason)

    def _assignment_load_stage_ids(self) -> set[str]:
        return parse_stage_ids(self.config.assignment_load_stage_ids)

    def _deal_counts_for_assignment_limit(self, deal: dict[str, Any]) -> bool:
        return is_assignment_load_deal(deal, self._assignment_load_stage_ids())

    def _stage_counts_for_assignment_limit(self, stage_id: str | None) -> bool:
        return stage_id_matches(stage_id, self._assignment_load_stage_ids())

    def _remember_created_deal_load(self, user_id: int | None, stage_id: str | None = None) -> None:
        """Project every newly-created deal that consumes the assignment limit.

        This is deliberately separate from owner selection. Historical director
        deals ignore the limit when they are assigned, but once they are created
        they still exist in the manager's New/In-work load and should be visible
        to later brand-new director assignments in the same run.
        """
        if not user_id:
            return
        stage_to_count = stage_id if stage_id is not None else self.config.deal_stage_id
        if not self._stage_counts_for_assignment_limit(stage_to_count):
            return
        self._projected_load_delta[user_id] += 1

    # Backward-compatible alias for older tests/scripts. It now counts any newly
    # created deal in configured load stages; the reason is ignored intentionally.
    def _remember_new_package_load(self, user_id: int | None, reason: str | None = None) -> None:
        self._remember_created_deal_load(user_id)

    def _list_eqazyna_companies_cached(self) -> list[dict[str, Any]]:
        if self._eqazyna_companies_cache is None:
            if not self.client:
                self._eqazyna_companies_cache = []
            else:
                self._eqazyna_companies_cache = self.client.list_all(
                    "crm.company.list",
                    {
                        "order": {"ID": "ASC"},
                        "filter": {"ORIGINATOR_ID": "EQAZYNA"},
                        "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "COMMENTS", "DATE_CREATE"],
                    },
                )
        return self._eqazyna_companies_cache

    def _list_eqazyna_deals_cached(self) -> list[dict[str, Any]]:
        if self._eqazyna_deals_cache is None:
            if not self.client:
                self._eqazyna_deals_cache = []
            else:
                list_method = self.client.list_eqazyna_deals
                if "reason_fields" in inspect.signature(list_method).parameters:
                    self._eqazyna_deals_cache = list_method(
                        reason_fields=self._failed_reason_fields(),
                    )
                else:
                    self._eqazyna_deals_cache = list_method()
        return self._eqazyna_deals_cache

    def _historical_sort_key(self, record: dict[str, Any]) -> tuple[datetime, int]:
        """Sort oldest CRM entities first, with ID as deterministic fallback."""
        raw_date = str(record.get("DATE_CREATE") or "").strip()
        parsed = datetime.max.replace(tzinfo=timezone.utc)
        if raw_date:
            try:
                parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            record_id = int(record.get("ID") or 0)
        except (TypeError, ValueError):
            record_id = 0
        return parsed, record_id

    def _package_owner_by_director(self, director: str | None) -> tuple[int | None, str | None]:
        """Return the historical owner of an existing e-Qazyna deal package.

        Production rule: only e-Qazyna deals create historical ownership for a
        director. Company owners, contact owners and legacy manual lists are not
        owner history for automatic distribution.

        A deal can be matched either by director text in its own comments or by
        a linked e-Qazyna company whose comments contain the same director. The
        owner anchor is still the deal owner, not the company owner.
        """
        director_keys = set(director_identity_keys(director))
        if not director_keys:
            return None, None

        matching_company_ids: set[str] = set()
        for company in self._list_eqazyna_companies_cached():
            company_director = _extract_director_from_comments(str(company.get("COMMENTS") or ""))
            if not director_keys.intersection(director_identity_keys(company_director)):
                continue
            company_id = str(company.get("ID") or "")
            if company_id:
                matching_company_ids.add(company_id)

        matching_deals: list[dict[str, Any]] = []
        for deal in self._list_eqazyna_deals_cached():
            deal_company_id = str(deal.get("COMPANY_ID") or "")
            deal_director = _extract_director_from_comments(str(deal.get("COMMENTS") or ""))
            matches_by_company = bool(deal_company_id and deal_company_id in matching_company_ids)
            matches_by_comment = bool(director_keys.intersection(director_identity_keys(deal_director)))
            if matches_by_company or matches_by_comment:
                matching_deals.append(deal)

        eligible_deals = [
            deal for deal in matching_deals
            if self._is_allowed_manager(self._record_assigned_by_id(deal))
            and not self._is_source_responsible(self._record_assigned_by_id(deal))
        ]
        if eligible_deals:
            oldest_deal = min(eligible_deals, key=self._historical_sort_key)
            owner_id = self._record_assigned_by_id(oldest_deal)
            return owner_id, "historical_first_deal_owner"

        return None, None

    def _manager_load(self, user_id: int) -> int:
        """Current e-Qazyna load for soft assignment balancing.

        The load table is built once per parser run. Previously every new
        director rescanned every e-Qazyna deal once for every active manager.
        That became needlessly expensive as the CRM history grew.
        """
        if self._manager_load_cache is None:
            counts: dict[int, int] = {}
            for deal in self._list_eqazyna_deals_cached():
                owner_id = self._record_assigned_by_id(deal)
                if owner_id is None:
                    continue
                if self._deal_counts_for_assignment_limit(deal):
                    counts[owner_id] = counts.get(owner_id, 0) + 1
            self._manager_load_cache = counts
        return int(self._manager_load_cache.get(user_id, 0))

    def _effective_manager_load(self, user_id: int) -> int:
        return self._manager_load(user_id) + int(self._projected_load_delta.get(user_id, 0))

    def _lowest_loaded_owner_from_current_load(self) -> tuple[int | None, str | None]:
        """Choose the least-loaded active manager deterministically.

        The previous implementation used random.choice among minimum-load
        candidates. That made dry-run logs noisy and could keep a low-load
        manager from being filled predictably when many packages were processed.
        This function is deliberately boring: lowest effective active-deal load
        wins; user ID is only a stable tie-breaker.
        """
        if not ALLOWED_USER_IDS:
            return None, None
        load = {user_id: self._effective_manager_load(user_id) for user_id in ALLOWED_USER_IDS}
        limit = max(0, int(self.config.assignment_limit_per_manager or 0))
        candidates = [user_id for user_id, current in load.items() if not limit or current < limit]
        if not candidates:
            target = min(ALLOWED_USER_IDS, key=lambda user_id: (load.get(user_id, 0), user_id))
            return target, "lowest_active_deal_load_limit_expanded"
        target = min(candidates, key=lambda user_id: (load.get(user_id, 0), user_id))
        return target, "lowest_active_deal_load_new_director"

    # Backward-compatible alias for older tests/scripts.
    def _random_owner_from_current_load(self) -> tuple[int | None, str | None]:
        return self._lowest_loaded_owner_from_current_load()

    def _special_owner_by_director_contact(self, director: str | None) -> tuple[int | None, str | None, str | None]:
        """Return a manually pinned owner for exceptional director contacts.

        This is an explicit exception layer for cases where a director/founder
        package must stay with a specific Bitrix user even when that user is not
        an active distribution manager in managers.yml. It deliberately runs
        before historical deal ownership and before load balancing.
        """
        if not SPECIAL_OWNER_BY_CONTACT_ID:
            return None, None, None

        fio = split_director_fio(director)
        if not fio:
            return None, None, None

        cache_key = fio.normalized
        if cache_key in self._director_pin_lookup_cache:
            return self._director_pin_lookup_cache[cache_key]

        result: tuple[int | None, str | None, str | None] = (None, None, None)
        if not self.client:
            self._director_pin_lookup_cache[cache_key] = result
            return result

        contact = None
        try:
            contact = self.client.find_contact_by_fio(fio.last_name, fio.name, fio.second_name)
            if not contact:
                find_alias = getattr(self.client, "find_contact_by_director_alias", None)
                if callable(find_alias):
                    contact = find_alias(fio.raw)
        except Exception:
            contact = None

        contact_id = self._record_id(contact)
        if contact_id is not None:
            target_owner_id = SPECIAL_OWNER_BY_CONTACT_ID.get(contact_id)
            if target_owner_id:
                result = (
                    target_owner_id,
                    f"special_director_contact_pin_{contact_id}",
                    str(contact_id),
                )

        self._director_pin_lookup_cache[cache_key] = result
        return result

    def _record_id(self, record: dict | None) -> int | None:
        if not record:
            return None
        try:
            value = record.get("ID")
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _resolve_target_responsible(
        self,
        app: Application,
        enr: CompanyEnrichment,
        company: dict | None,
    ) -> tuple[int | None, str | None]:
        """Resolve one strict owner for the new application package.

        Canonical rule: director/founder history wins. Manual YAML overrides
        and BIN-level overrides are intentionally not used in the automatic
        pipeline, because they can split one director package across managers.
        Manual movement is handled only by the admin reassignment workflow.
        """
        director = enr.director

        special_owner_id, special_reason, special_contact_id = self._special_owner_by_director_contact(director)
        if special_owner_id:
            self._remember_assignment(app.bin, director, special_owner_id, special_reason)
            return special_owner_id, special_reason

        cached_owner_id, cached_reason = self._cached_assignment(app.bin, director)
        director_owner_id, director_reason = self._package_owner_by_director(director)

        if cached_owner_id:
            if director_owner_id and director_owner_id != cached_owner_id:
                raise BitrixError(
                    f"DIRECTOR_PACKAGE_OWNER_CONFLICT: director {director!r} is cached for "
                    f"{cached_owner_id} ({self._user_name(cached_owner_id)}) but CRM package owner is "
                    f"{director_owner_id} ({self._user_name(director_owner_id)})."
                )
            return cached_owner_id, f"{cached_reason}_runtime_cache"

        if director_owner_id:
            self._remember_assignment(app.bin, director, director_owner_id, director_reason)
            return director_owner_id, director_reason

        # Fully new director/BIN: choose by the lowest e-Qazyna deal load in the configured limit stages.
        # Existing company/contact owners are deliberately ignored here: only e-Qazyna deal history
        # can anchor a director package. Manual movement belongs to Reassign CRM owner.
        # The limit is soft: if everyone is at/above it, choose the lowest load.
        owner_id, reason = self._lowest_loaded_owner_from_current_load()
        self._remember_assignment(app.bin, director, owner_id, reason)
        return owner_id, reason

    def ensure_director_contact(self, company_id: str, deal_id: str | None, enr: CompanyEnrichment, responsible_id: int | None = None) -> tuple[str | None, str | None, str | None]:
        """Create/find director contact and link it to company and deal.

        Director data comes from eGov. Phone from eGov address belongs to company,
        not to the physical person, so it is intentionally not written to contact.
        """
        fio = split_director_fio(enr.director)
        if not fio:
            return None, "director_empty", None
        if self.config.dry_run:
            return "DRY_RUN_CONTACT", "dry_run_director_contact", None
        try:
            cache_keys = director_identity_keys(fio.raw) or [fio.normalized]
            cached_id = next((self._director_contact_cache.get(key) for key in cache_keys if self._director_contact_cache.get(key)), None)
            if cached_id:
                contact_id = cached_id
                action_parts = ["cached_contact"]
            else:
                contact = self.client.find_contact_by_fio(fio.last_name, fio.name, fio.second_name)
                if not contact:
                    find_alias = getattr(self.client, "find_contact_by_director_alias", None)
                    if callable(find_alias):
                        contact = find_alias(fio.raw)
                if contact:
                    contact_id = str(contact["ID"])
                    action_parts = ["existing_contact"]
                    # Backfill position and enforce the same owner as the whole
                    # director package. Contact/company/deal must not diverge.
                    update_fields: dict[str, object] = {}
                    if not contact.get("POST"):
                        update_fields["POST"] = "Руководитель"
                    contact_owner_id = self._record_assigned_by_id(contact)
                    if responsible_id and contact_owner_id != responsible_id:
                        update_fields["ASSIGNED_BY_ID"] = int(responsible_id)
                    if update_fields:
                        self.client.update_contact(contact_id, update_fields)
                else:
                    fields: dict[str, object] = {
                        "LAST_NAME": fio.last_name,
                        "NAME": fio.name,
                        "SECOND_NAME": fio.second_name,
                        "POST": "Руководитель",
                        "SOURCE_ID": "OTHER",
                        "SOURCE_DESCRIPTION": "eGov / e-Qazyna",
                        "COMMENTS": f"Руководитель из eGov. Исходное ФИО: {fio.raw}",
                        "OPENED": "Y",
                        "COMPANY_ID": int(company_id),
                    }
                    contact_owner_id = responsible_id or self._configured_assigned_by_id()
                    if contact_owner_id:
                        fields["ASSIGNED_BY_ID"] = int(contact_owner_id)
                    contact_id = self.client.create_contact(fields)
                    action_parts = ["created_contact"]
                for cache_key in cache_keys:
                    self._director_contact_cache[cache_key] = contact_id

            company_linked = self.client.link_contact_to_company(company_id, contact_id, primary=True)
            action_parts.append("linked_company" if company_linked else "company_already_linked")
            if deal_id:
                deal_linked = self.client.link_contact_to_deal(deal_id, contact_id, primary=True)
                action_parts.append("linked_deal" if deal_linked else "deal_already_linked")
            return contact_id, "+".join(action_parts), None
        except Exception as exc:  # noqa: BLE001
            return None, "contact_error", str(exc)

    def _phone_multifield(self, enr: CompanyEnrichment) -> list[dict[str, str]] | None:
        if not enr.phone:
            return None
        return [{"VALUE": enr.phone, "VALUE_TYPE": "WORK"}]

    def _company_contact_fields(self, enr: CompanyEnrichment) -> dict[str, object]:
        fields: dict[str, object] = {}
        phone = self._phone_multifield(enr)
        if phone:
            fields["PHONE"] = phone
        return fields

    def _company_address_fields(self, enr: CompanyEnrichment) -> dict[str, object]:
        """Fill both legal and visible address blocks.

        Bitrix deal cards often show the visible ADDRESS block, while legal data
        is stored in REG_ADDRESS*. If we fill only REG_ADDRESS, the UI can show
        just "Казахстан" or an almost empty address. Until a separate factual
        address source is connected, use the eGov legal address in both blocks.
        """
        # Do not overwrite existing Bitrix address with empty data when eGov
        # enrichment is missing/rejected by name+OKED validation.
        if not enr.legal_address:
            return {}
        address = enr.legal_address
        city = enr.city or ""
        region = enr.region or ""
        return {
            "REG_ADDRESS": address,
            "REG_ADDRESS_CITY": city,
            "REG_ADDRESS_PROVINCE": region,
            "REG_ADDRESS_COUNTRY": "Казахстан",
            "ADDRESS": address,
            "ADDRESS_CITY": city,
            "ADDRESS_PROVINCE": region,
            "ADDRESS_COUNTRY": "Казахстан",
        }

    def _company_update_fields(self, app: Application, enr: CompanyEnrichment) -> dict[str, object]:
        fields: dict[str, object] = {
            "COMMENTS": build_company_summary(app, enr),
            **self._company_address_fields(enr),
            **self._company_contact_fields(enr),
        }
        # Prefer official eGov name when it is available; otherwise keep e-Qazyna name.
        if enr.name or app.applicant_name:
            fields["TITLE"] = enr.name or app.applicant_name
        return fields

    def _company_fields(self, app: Application, enr: CompanyEnrichment, responsible_id: int | None = None) -> dict[str, object]:
        name = enr.name or app.applicant_name
        fields: dict[str, object] = {
            "TITLE": name,
            "COMPANY_TYPE": "CUSTOMER",
            "ORIGINATOR_ID": "EQAZYNA",
            "ORIGIN_ID": app.bin,
            "COMMENTS": build_company_summary(app, enr),
            "OPENED": "Y",
            **self._company_address_fields(enr),
            **self._company_contact_fields(enr),
        }
        responsible = responsible_id if responsible_id is not None else self._configured_assigned_by_id()
        if responsible:
            fields["ASSIGNED_BY_ID"] = responsible
        return fields

    def _failed_stage_ids(self) -> set[str]:
        raw = self.config.failed_deal_stage_ids or ""
        return parse_stage_ids(raw)

    def _target_failed_stage_id(self) -> str:
        # Business requirement: when a failed package is inherited, write the plain
        # configured stage (usually LOSE), not a category-prefixed source stage such as C2:LOSE.
        raw = self.config.failed_deal_stage_ids or "LOSE"
        for part in str(raw).split(","):
            stage = part.strip()
            if stage:
                return stage
        return "LOSE"

    def _failed_reason_fields(self) -> list[str]:
        raw = self.config.failed_deal_reason_fields or ""
        fields = [part.strip() for part in raw.split(",") if part.strip()]
        return fields or ["COMMENTS"]

    def _string_value(self, value: Any) -> str | None:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [self._string_value(item) for item in value]
            text = "; ".join(part for part in parts if part)
            return text or None
        if isinstance(value, dict):
            for key in ("VALUE", "value", "TEXT", "text", "NAME", "name", "TITLE", "title"):
                if key in value:
                    text = self._string_value(value.get(key))
                    if text:
                        return text
            return str(value)
        return str(value)

    def _is_failed_deal(self, deal: dict[str, Any]) -> bool:
        stage_id = str(deal.get("STAGE_ID") or "").strip().upper()
        semantic = str(deal.get("STAGE_SEMANTIC_ID") or "").strip().upper()
        if semantic == "F":
            return True
        if stage_id and stage_id_matches(stage_id, self._failed_stage_ids()):
            return True
        return False

    def _failed_reason_from_deal(self, deal: dict[str, Any]) -> tuple[str | None, str | None]:
        for field_name in self._failed_reason_fields():
            value = self._string_value(deal.get(field_name))
            if value:
                return value, field_name
        return None, None

    def _failed_deal_inheritance_for_director(self, director: str | None) -> FailedDealInheritance | None:
        if not self.config.inherit_failed_deals_by_director:
            return None
        director_key = director_identity_key(director)
        director_keys = set(director_identity_keys(director))
        if not director_key or not director_keys:
            return None
        if director_key in self._failed_deal_by_director_cache:
            return self._failed_deal_by_director_cache[director_key]

        company_ids: set[str] = set()
        for company in self._list_eqazyna_companies_cached():
            company_director = _extract_director_from_comments(str(company.get("COMMENTS") or ""))
            if director_keys.intersection(director_identity_keys(company_director)):
                if company.get("ID") is not None:
                    company_ids.add(str(company.get("ID")))

        failed_deals: list[dict[str, Any]] = []
        for deal in self._list_eqazyna_deals_cached():
            deal_company_id = str(deal.get("COMPANY_ID") or "")
            deal_director = _extract_director_from_comments(str(deal.get("COMMENTS") or ""))
            matches_by_company = bool(deal_company_id and deal_company_id in company_ids)
            matches_by_comment = bool(director_keys.intersection(director_identity_keys(deal_director)))
            if not (matches_by_company or matches_by_comment):
                continue
            if self._is_failed_deal(deal):
                failed_deals.append(deal)

        if not failed_deals:
            self._failed_deal_by_director_cache[director_key] = None
            return None

        def sort_key(deal: dict[str, Any]) -> tuple[int, str]:
            try:
                deal_id = int(deal.get("ID") or 0)
            except (TypeError, ValueError):
                deal_id = 0
            return deal_id, str(deal.get("CLOSEDATE") or deal.get("DATE_MODIFY") or "")

        source_deal = sorted(failed_deals, key=sort_key, reverse=True)[0]
        reason, reason_field = self._failed_reason_from_deal(source_deal)
        inheritance = FailedDealInheritance(
            stage_id=self._target_failed_stage_id(),
            source_stage_id=str(source_deal.get("STAGE_ID") or "") or None,
            reason=reason,
            reason_field=reason_field,
            source_deal_id=str(source_deal.get("ID")) if source_deal.get("ID") is not None else None,
            source_deal_title=self._string_value(source_deal.get("TITLE")),
        )
        self._failed_deal_by_director_cache[director_key] = inheritance
        return inheritance

    def _deal_fields(
        self,
        app: Application,
        enr: CompanyEnrichment,
        company_id: str,
        responsible_id: int | None = None,
        failed_inheritance: FailedDealInheritance | None = None,
    ) -> dict[str, object]:
        comments = build_deal_comment(app, enr)
        fields: dict[str, object] = {
            "TITLE": build_deal_title(app, enr),
            "COMPANY_ID": int(company_id),
            "CATEGORY_ID": int(self.config.deal_category_id or 0),
            "STAGE_ID": self.config.deal_stage_id,
            "OPENED": "Y",
            "CLOSED": "N",
            "COMMENTS": comments,
            "ORIGINATOR_ID": "EQAZYNA",
            "ORIGIN_ID": app.application_key,
            "SOURCE_ID": "OTHER",
            "SOURCE_DESCRIPTION": "e-Qazyna minerals registry",
        }
        if failed_inheritance:
            fields["STAGE_ID"] = failed_inheritance.stage_id
            fields["CLOSED"] = "Y"
            reason_text = failed_inheritance.reason or "причина не заполнена в старой сделке"
            fields["COMMENTS"] = (
                f"{comments}\n\n"
                "Автозавершение по правилу руководителя:\n"
                f"у этого руководителя уже есть сделка в финальной стадии '{failed_inheritance.source_stage_id or failed_inheritance.stage_id}'.\n"
                f"Старая сделка: {failed_inheritance.source_deal_id or 'не определена'}"
                f"{(' — ' + failed_inheritance.source_deal_title) if failed_inheritance.source_deal_title else ''}.\n"
                f"Наследованная причина: {reason_text}"
            )
            if failed_inheritance.reason_field and failed_inheritance.reason_field != "COMMENTS" and failed_inheritance.reason is not None:
                fields[failed_inheritance.reason_field] = failed_inheritance.reason
        responsible = responsible_id if responsible_id is not None else self._configured_assigned_by_id()
        if responsible:
            fields["ASSIGNED_BY_ID"] = responsible
        return fields

    def ensure_requisite(self, company_id: str, app: Application, enr: CompanyEnrichment) -> str | None:
        bin_field = self.config.requisite_bin_field or "RQ_BIN"
        try:
            existing = self.client.list_requisites_for_company(company_id)
            fields = self._requisite_fields(company_id, app, enr)
            for req in existing:
                if str(req.get(bin_field) or req.get("RQ_BIN") or req.get("RQ_INN") or "").strip() == app.bin:
                    req_id = str(req.get("ID"))
                    update_fields = {k: v for k, v in fields.items() if k not in {"ENTITY_TYPE_ID", "ENTITY_ID", "PRESET_ID"}}
                    if update_fields:
                        self.client.update_requisite(req_id, update_fields)
                    return req_id
            if existing:
                first_id = str(existing[0].get("ID"))
                if fields:
                    update_fields = {k: v for k, v in fields.items() if k not in {"ENTITY_TYPE_ID", "ENTITY_ID", "PRESET_ID"}}
                    self.client.update_requisite(first_id, update_fields)
                return first_id
            if not self.config.requisite_preset_id:
                return None
            return self.client.add_requisite(fields)
        except BitrixError:
            raise
        except Exception:
            return None

    def _requisite_fields(self, company_id: str, app: Application, enr: CompanyEnrichment) -> dict[str, object]:
        if not self.config.requisite_preset_id:
            return {}
        name = enr.name or app.applicant_name
        # Bitrix shows the requisites row by its visible name/company-name fields.
        # Put BIN into that visible line, while keeping legal company name in full-name.
        visible = f"БИН {app.bin} — {name}"
        fields: dict[str, object] = {
            "ENTITY_TYPE_ID": 4,
            "ENTITY_ID": int(company_id),
            "PRESET_ID": int(self.config.requisite_preset_id),
            "NAME": visible,
            "ACTIVE": "Y",
            "ADDRESS_ONLY": "N",
            "SORT": 500,
            "RQ_COMPANY_NAME": visible,
            "RQ_COMPANY_FULL_NAME": name,
            "RQ_DIRECTOR": enr.director or "",
            "ORIGINATOR_ID": "EQAZYNA",
            "XML_ID": f"EQAZYNA-REQ-{app.bin}",
        }
        fields[self.config.requisite_bin_field or "RQ_BIN"] = app.bin
        # Some Bitrix24 Kazakhstan portals expose RQ_BIN; others can be configured through BITRIX_REQUISITE_BIN_FIELD.
        return fields
