from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .distribute_companies import (
    ALLOWED_USER_IDS,
    HARD_BIN_OWNERS,
    USER_NAMES,
    _extract_director_from_comments,
    _normalize_bin,
    _normalize_text,
    _to_int,
)
from .settings import Settings


ORIGINATOR_ID = "EQAZYNA"
DEFAULT_SOURCE_RESPONSIBLE_IDS = "36,44"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit and repair all e-Qazyna deal packages in Bitrix. "
            "No per-manager or batch limits are applied."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Only calculate changes, do not write to Bitrix")
    parser.add_argument("--out", default="exports/audit_repair_deal_packages_log.json")
    parser.add_argument("--csv-out", default="exports/audit_repair_deal_packages_groups.csv")
    parser.add_argument(
        "--source-responsible-ids",
        default=DEFAULT_SOURCE_RESPONSIBLE_IDS,
        help="Comma-separated technical/source user IDs to move away from, e.g. 36,44",
    )
    parser.add_argument(
        "--include-closed-deals",
        action="store_true",
        help="Also rewrite closed deals. Default: audit them, but do not change them.",
    )
    parser.add_argument(
        "--sync-contacts",
        action="store_true",
        help="Also rewrite linked contacts to the package target owner. Slower, but cleaner.",
    )
    parser.add_argument(
        "--repair-scope",
        choices=["split_only", "split_and_hard", "hard_bin_only", "all_actions"],
        default="split_and_hard",
        help=(
            "What to repair: split_only = only packages with mixed owners; "
            "split_and_hard = mixed owners plus hard BIN packages that are on the wrong owner; "
            "hard_bin_only = only hard BIN packages on the wrong owner; "
            "all_actions = also move packages from technical/source owners to lowest-load managers."
        ),
    )
    parser.add_argument(
        "--duplicate-hard-bin-policy",
        choices=["skip", "current_owner_majority"],
        default="skip",
        help=(
            "How to handle a BIN that is configured for several hard owners. "
            "skip = do not change such groups and log them as unresolved; "
            "current_owner_majority = choose by current owner/majority/load."
        ),
    )
    parser.add_argument(
        "--limit-per-manager-companies",
        type=int,
        default=15,
        help=(
            "Soft client/company limit for automatic new-package assignment. "
            "A manager at or above this limit before assignment is skipped. 0 = ignore."
        ),
    )
    parser.add_argument(
        "--limit-per-manager-active-deals",
        type=int,
        default=80,
        help=(
            "Soft active e-Qazyna deal limit for automatic new-package assignment. "
            "A manager at or above this limit before assignment is skipped. 0 = ignore."
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional seed for tie-breaks")
    return parser.parse_args()


def parse_id_set(raw: Any) -> set[int]:
    ids: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise SystemExit(f"Invalid user ID: {part!r}") from exc
    return ids


def safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def user_name(user_id: int | None) -> str:
    if user_id is None:
        return "None"
    return USER_NAMES.get(user_id, f"User {user_id}")


def owner_id(entity: dict[str, Any]) -> int | None:
    return _to_int(entity.get("ASSIGNED_BY_ID"))


def is_closed_deal(deal: dict[str, Any]) -> bool:
    return safe_str(deal.get("CLOSED")).upper() == "Y"


def extract_bin_from_origin_id(origin_id: Any) -> str:
    text = safe_str(origin_id)
    # eQazyna|42612-NEA|260240008546
    parts = [p.strip() for p in text.split("|")]
    for part in reversed(parts):
        bin_value = _normalize_bin(part)
        if bin_value:
            return bin_value
    return ""


def extract_bin_from_requisites(requisites: list[dict[str, Any]]) -> str:
    for req in requisites:
        for field in ("RQ_BIN", "RQ_INN"):
            bin_value = _normalize_bin(req.get(field))
            if bin_value:
                return bin_value
    return ""


def extract_director(entity: dict[str, Any]) -> str:
    return _extract_director_from_comments(safe_str(entity.get("COMMENTS")))


def entity_label(entity: dict[str, Any]) -> str:
    return safe_str(entity.get("TITLE") or entity.get("NAME") or entity.get("ID"))


def list_all_eqazyna_deals(client: BitrixClient) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": ORIGINATOR_ID},
            "select": [
                "ID",
                "TITLE",
                "COMPANY_ID",
                "CONTACT_ID",
                "ASSIGNED_BY_ID",
                "STAGE_ID",
                "CATEGORY_ID",
                "CLOSED",
                "COMMENTS",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
            ],
        },
    )


def list_all_eqazyna_companies(client: BitrixClient) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": ORIGINATOR_ID},
            "select": [
                "ID",
                "TITLE",
                "ASSIGNED_BY_ID",
                "COMMENTS",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
            ],
        },
    )


def get_company(client: BitrixClient, company_id: str) -> dict[str, Any] | None:
    if not company_id or company_id == "0":
        return None
    result = client.call("crm.company.get", {"id": int(company_id)})
    return result if isinstance(result, dict) else None


def get_contact(client: BitrixClient, contact_id: str) -> dict[str, Any] | None:
    if not contact_id or contact_id == "0":
        return None
    result = client.call("crm.contact.get", {"id": int(contact_id)})
    return result if isinstance(result, dict) else None


def update_company_owner(client: BitrixClient, company_id: str, target_user_id: int) -> None:
    client.update_company(company_id, {"ASSIGNED_BY_ID": target_user_id})


def update_deal_owner(client: BitrixClient, deal_id: str, target_user_id: int) -> None:
    client.update_deal(deal_id, {"ASSIGNED_BY_ID": target_user_id})


def update_contact_owner(client: BitrixClient, contact_id: str, target_user_id: int) -> None:
    client.update_contact(contact_id, {"ASSIGNED_BY_ID": target_user_id})


def build_company_index(
    client: BitrixClient,
    deals: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    companies: dict[str, dict[str, Any]] = {}

    for company in list_all_eqazyna_companies(client):
        company_id = safe_str(company.get("ID"))
        if company_id:
            companies[company_id] = company

    deal_company_ids = sorted({safe_str(deal.get("COMPANY_ID")) for deal in deals if safe_str(deal.get("COMPANY_ID")) not in {"", "0"}}, key=lambda x: int(x))

    for company_id in deal_company_ids:
        if company_id in companies:
            continue
        company = get_company(client, company_id)
        if company:
            companies[company_id] = company

    for company_id, company in list(companies.items()):
        bin_value = _normalize_bin(company.get("ORIGIN_ID"))
        if not bin_value:
            try:
                requisites = client.list_requisites_for_company(company_id)
                bin_value = extract_bin_from_requisites(requisites)
            except Exception as exc:  # noqa: BLE001
                company["_requisite_error"] = str(exc)
                bin_value = ""
        company["_bin"] = bin_value
        company["_director"] = extract_director(company)

    return companies


def record_bin(record: dict[str, Any]) -> str:
    return safe_str(record.get("bin"))


def record_director(record: dict[str, Any]) -> str:
    return safe_str(record.get("director"))


def make_records(
    deals: list[dict[str, Any]],
    companies: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    records: list[dict[str, Any]] = []
    company_deals: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for deal in deals:
        company_id = safe_str(deal.get("COMPANY_ID"))
        company = companies.get(company_id) if company_id else None
        bin_value = extract_bin_from_origin_id(deal.get("ORIGIN_ID"))
        if not bin_value and company:
            bin_value = safe_str(company.get("_bin"))
        director = extract_director(deal)
        if not director and company:
            director = safe_str(company.get("_director"))
        records.append(
            {
                "entity_type": "deal",
                "entity": deal,
                "entity_id": safe_str(deal.get("ID")),
                "company_id": company_id,
                "bin": bin_value,
                "director": director,
                "owner_id": owner_id(deal),
                "closed": is_closed_deal(deal),
                "title": entity_label(deal),
            }
        )
        if company_id:
            company_deals[company_id].append(deal)

    for company_id, company in companies.items():
        # Add company as its own record if it belongs to e-Qazyna flow or is referenced by e-Qazyna deals.
        if safe_str(company.get("ORIGINATOR_ID")) != ORIGINATOR_ID and company_id not in company_deals:
            continue
        records.append(
            {
                "entity_type": "company",
                "entity": company,
                "entity_id": company_id,
                "company_id": company_id,
                "bin": safe_str(company.get("_bin")),
                "director": safe_str(company.get("_director")),
                "owner_id": owner_id(company),
                "closed": False,
                "title": entity_label(company),
            }
        )

    return records, companies, company_deals


def group_key_for_record(record: dict[str, Any]) -> tuple[str, str, str]:
    bin_value = record_bin(record)
    director = record_director(record)

    # Hard BINs are absolute. They are grouped by BIN first, so a hard BIN cannot be dragged away by another director package.
    if bin_value and bin_value in HARD_BIN_OWNERS:
        return f"hard_bin|{bin_value}", "hard_bin", bin_value

    if director:
        return f"director|{_normalize_text(director)}", "director", director

    if bin_value:
        return f"bin|{bin_value}", "bin", bin_value

    company_id = safe_str(record.get("company_id"))
    if company_id and company_id != "0":
        return f"company|{company_id}", "company", company_id

    return f"deal|{record.get('entity_id')}", "deal", safe_str(record.get("title"))


def build_groups(records: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, str]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, dict[str, str]] = {}
    for record in records:
        key, group_type, readable = group_key_for_record(record)
        groups[key].append(record)
        meta[key] = {"group_type": group_type, "readable_name": readable}
    return groups, meta


def initial_load(companies: dict[str, dict[str, Any]]) -> dict[int, int]:
    load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    for company in companies.values():
        user_id = owner_id(company)
        if user_id in load:
            load[user_id] += 1
    return load


def initial_active_deal_load(records: list[dict[str, Any]]) -> dict[int, int]:
    load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    for record in records:
        if record.get("entity_type") != "deal":
            continue
        if record.get("closed"):
            continue
        user_id = record.get("owner_id")
        if user_id in load:
            load[user_id] += 1
    return load


def choose_lowest_load(
    client_load: dict[int, int],
    active_deal_load: dict[int, int],
    limit_per_manager_companies: int,
    limit_per_manager_active_deals: int,
) -> tuple[int | None, dict[str, Any]]:
    eligible: list[int] = []
    rejected: dict[str, dict[str, Any]] = {}

    for user_id in ALLOWED_USER_IDS:
        current_clients = client_load.get(user_id, 0)
        current_active_deals = active_deal_load.get(user_id, 0)
        reject_reasons: list[str] = []

        if limit_per_manager_companies and limit_per_manager_companies > 0:
            if current_clients >= limit_per_manager_companies:
                reject_reasons.append("company_limit_reached")

        if limit_per_manager_active_deals and limit_per_manager_active_deals > 0:
            if current_active_deals >= limit_per_manager_active_deals:
                reject_reasons.append("active_deal_limit_reached")

        if reject_reasons:
            rejected[str(user_id)] = {
                "user_name": user_name(user_id),
                "client_load": current_clients,
                "active_deal_load": current_active_deals,
                "reasons": reject_reasons,
            }
        else:
            eligible.append(user_id)

    debug: dict[str, Any] = {
        "limit_per_manager_companies": limit_per_manager_companies,
        "limit_per_manager_active_deals": limit_per_manager_active_deals,
        "eligible_user_ids": eligible,
        "eligible_user_names": [user_name(user_id) for user_id in eligible],
        "rejected_users": rejected,
    }

    if not eligible:
        return None, debug

    min_active_deals = min(active_deal_load.get(user_id, 0) for user_id in eligible)
    active_candidates = [
        user_id
        for user_id in eligible
        if active_deal_load.get(user_id, 0) == min_active_deals
    ]

    min_clients = min(client_load.get(user_id, 0) for user_id in active_candidates)
    client_candidates = [
        user_id
        for user_id in active_candidates
        if client_load.get(user_id, 0) == min_clients
    ]

    target = random.choice(sorted(client_candidates))
    debug.update(
        {
            "selected_by": "below_limits_lowest_active_deal_load_then_lowest_client_load_then_random",
            "selected_user_id": target,
            "selected_user_name": user_name(target),
            "selected_active_deal_load": active_deal_load.get(target, 0),
            "selected_client_load": client_load.get(target, 0),
        }
    )
    return target, debug


def duplicate_hard_owner_map(bins: set[str]) -> dict[str, list[int]]:
    conflicts: dict[str, list[int]] = {}
    for bin_value in sorted(bins):
        owner_ids = sorted(set(HARD_BIN_OWNERS.get(bin_value, [])))
        if len(owner_ids) > 1:
            conflicts[bin_value] = owner_ids
    return conflicts


def is_duplicate_hard_bin(bin_value: str) -> bool:
    return len(set(HARD_BIN_OWNERS.get(bin_value, []))) > 1


def choose_from_candidates_by_current_ownership(
    candidates: set[int],
    owners: list[int],
    load: dict[int, int],
) -> tuple[int, str]:
    current = Counter(owner for owner in owners if owner in candidates)
    if current:
        top = current.most_common()
        top_count = top[0][1]
        top_candidates = {owner for owner, count in top if count == top_count}
        if len(top_candidates) == 1:
            return next(iter(top_candidates)), "current_owner_majority"
        candidates = top_candidates

    min_load = min(load.get(user_id, 0) for user_id in candidates)
    load_candidates = sorted(user_id for user_id in candidates if load.get(user_id, 0) == min_load)
    return random.choice(load_candidates), "lowest_load_tie_break"


def choose_hard_owner(
    bins: set[str],
    owners: list[int],
    load: dict[int, int],
) -> tuple[int, str, dict[str, Any]]:
    counts: Counter[int] = Counter()
    hard_bins: dict[str, list[int]] = {}
    for bin_value in sorted(bins):
        owner_ids = HARD_BIN_OWNERS.get(bin_value, [])
        if not owner_ids:
            continue
        hard_bins[bin_value] = owner_ids
        for owner_id in owner_ids:
            counts[owner_id] += 1

    if not counts:
        raise ValueError("choose_hard_owner called without hard BIN candidates")

    top_count = counts.most_common(1)[0][1]
    candidates = {owner_id for owner_id, count in counts.items() if count == top_count}

    if len(candidates) == 1:
        target = next(iter(candidates))
        return target, "hard_bin_owner", {"hard_bins": hard_bins, "candidate_counts": dict(counts)}

    target, tie_reason = choose_from_candidates_by_current_ownership(candidates, owners, load)
    return target, f"hard_bin_owner_tie_{tie_reason}", {"hard_bins": hard_bins, "candidate_counts": dict(counts)}


def build_director_hints(
    records: list[dict[str, Any]],
    load: dict[int, int],
    source_ids: set[int],
) -> dict[str, int]:
    hints: dict[str, int] = {}
    buckets: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for record in records:
        director = record_director(record)
        if not director:
            continue
        normalized = _normalize_text(director)
        bin_value = record_bin(record)
        owner = record.get("owner_id")

        if bin_value in HARD_BIN_OWNERS and not is_duplicate_hard_bin(bin_value):
            target, _reason, _debug = choose_hard_owner({bin_value}, [owner] if owner else [], load)
            buckets[normalized].append((target, "hard_bin_hint"))
        elif owner in ALLOWED_USER_IDS and owner not in source_ids:
            buckets[normalized].append((owner, "existing_owner_hint"))

    for director_key, values in buckets.items():
        counts = Counter(owner for owner, _reason in values)
        if not counts:
            continue
        top_count = counts.most_common(1)[0][1]
        candidates = {owner for owner, count in counts.items() if count == top_count}
        if len(candidates) == 1:
            hints[director_key] = next(iter(candidates))
        else:
            min_load = min(load.get(owner, 0) for owner in candidates)
            best = sorted(owner for owner in candidates if load.get(owner, 0) == min_load)[0]
            hints[director_key] = best

    return hints


def choose_target(
    group_records: list[dict[str, Any]],
    load: dict[int, int],
    active_deal_load: dict[int, int],
    source_ids: set[int],
    director_hints: dict[str, int],
    limit_per_manager_companies: int,
    limit_per_manager_active_deals: int,
) -> tuple[int | None, str, dict[str, Any]]:
    bins = {record_bin(record) for record in group_records if record_bin(record)}
    directors = {record_director(record) for record in group_records if record_director(record)}
    owners = [record.get("owner_id") for record in group_records if isinstance(record.get("owner_id"), int)]
    hard_bins = {bin_value for bin_value in bins if bin_value in HARD_BIN_OWNERS}

    if hard_bins:
        return choose_hard_owner(hard_bins, owners, load)

    director_targets = set()
    for director in directors:
        hint = director_hints.get(_normalize_text(director))
        if hint in ALLOWED_USER_IDS:
            director_targets.add(hint)

    if len(director_targets) == 1:
        target = next(iter(director_targets))
        return target, "existing_director_package_owner", {"director_targets": sorted(director_targets)}
    if len(director_targets) > 1:
        target, tie_reason = choose_from_candidates_by_current_ownership(director_targets, owners, load)
        return target, f"existing_director_package_owner_tie_{tie_reason}", {"director_targets": sorted(director_targets)}

    allowed_owners = [owner for owner in owners if owner in ALLOWED_USER_IDS and owner not in source_ids]
    if allowed_owners:
        counts = Counter(allowed_owners)
        top_count = counts.most_common(1)[0][1]
        candidates = {owner for owner, count in counts.items() if count == top_count}
        if len(candidates) == 1:
            target = next(iter(candidates))
            reason = "existing_package_owner" if len(counts) == 1 else "split_package_majority_owner"
            return target, reason, {"owner_counts": dict(counts)}
        target, tie_reason = choose_from_candidates_by_current_ownership(candidates, owners, load)
        return target, f"split_package_tie_{tie_reason}", {"owner_counts": dict(counts)}

    target, limit_debug = choose_lowest_load(
        client_load=load,
        active_deal_load=active_deal_load,
        limit_per_manager_companies=limit_per_manager_companies,
        limit_per_manager_active_deals=limit_per_manager_active_deals,
    )
    if target is None:
        return None, "skip_no_available_manager_below_limits", limit_debug
    return target, "no_allowed_owner_below_limits_lowest_load", limit_debug


def collect_group_contacts(client: BitrixClient, group_records: list[dict[str, Any]]) -> set[str]:
    contact_ids: set[str] = set()
    company_ids = {safe_str(record.get("company_id")) for record in group_records if safe_str(record.get("company_id")) not in {"", "0"}}
    deal_ids = {safe_str(record.get("entity_id")) for record in group_records if record.get("entity_type") == "deal"}

    for record in group_records:
        if record.get("entity_type") == "deal":
            contact_id = safe_str(record.get("entity", {}).get("CONTACT_ID"))
            if contact_id and contact_id != "0":
                contact_ids.add(contact_id)

    for company_id in company_ids:
        try:
            contact_ids.update(client.company_contact_ids(company_id))
        except Exception:  # noqa: BLE001
            pass

    for deal_id in deal_ids:
        try:
            contact_ids.update(client.deal_contact_ids(deal_id))
        except Exception:  # noqa: BLE001
            pass

    return contact_ids


def skipped_duplicate_hard_bin_row(
    group_key: str,
    group_type: str,
    readable_name: str,
    group_records: list[dict[str, Any]],
    conflicts: dict[str, list[int]],
) -> dict[str, Any]:
    company_records = {
        safe_str(record.get("company_id")): record
        for record in group_records
        if record.get("entity_type") == "company" and safe_str(record.get("company_id"))
    }
    deal_records = [record for record in group_records if record.get("entity_type") == "deal"]
    owners_before = sorted({record.get("owner_id") for record in group_records if isinstance(record.get("owner_id"), int)})
    bins = sorted({record_bin(record) for record in group_records if record_bin(record)})
    directors = sorted({record_director(record) for record in group_records if record_director(record)})
    conflict_names = {
        bin_value: [user_name(owner_id) for owner_id in owner_ids]
        for bin_value, owner_ids in conflicts.items()
    }

    row: dict[str, Any] = {
        "group_key": group_key,
        "group_type": group_type,
        "readable_name": readable_name,
        "bins": bins,
        "directors": directors,
        "target_user_id": None,
        "target_user_name": "UNRESOLVED_DUPLICATE_HARD_BIN",
        "reason": "skip_duplicate_hard_bin_conflict",
        "reason_debug": {
            "duplicate_hard_bins": conflicts,
            "duplicate_hard_bin_names": conflict_names,
        },
        "owners_before": owners_before,
        "owners_before_names": [user_name(owner) for owner in owners_before],
        "is_split_before": len(set(owners_before)) > 1,
        "company_count": len(company_records),
        "deal_count": len(deal_records),
        "actions": [],
        "errors": [],
    }

    for record in sorted(group_records, key=lambda item: (safe_str(item.get("entity_type")), int(item.get("entity_id") or 0))):
        old_owner = record.get("owner_id")
        row["actions"].append(
            {
                "entity_type": record.get("entity_type"),
                "entity_id": record.get("entity_id"),
                "title": record.get("title"),
                "company_id": record.get("company_id"),
                "closed": bool(record.get("closed")),
                "old_assigned_by_id": old_owner,
                "old_assigned_by_name": user_name(old_owner),
                "new_assigned_by_id": None,
                "new_assigned_by_name": "UNRESOLVED_DUPLICATE_HARD_BIN",
                "action": "skip_duplicate_hard_bin_conflict",
                "error": None,
            }
        )

    return row


def skipped_no_available_manager_row(
    group_key: str,
    group_type: str,
    readable_name: str,
    group_records: list[dict[str, Any]],
    reason: str,
    reason_debug: dict[str, Any],
) -> dict[str, Any]:
    company_records = {
        safe_str(record.get("company_id")): record
        for record in group_records
        if record.get("entity_type") == "company" and safe_str(record.get("company_id"))
    }
    deal_records = [record for record in group_records if record.get("entity_type") == "deal"]
    owners_before = sorted({record.get("owner_id") for record in group_records if isinstance(record.get("owner_id"), int)})
    bins = sorted({record_bin(record) for record in group_records if record_bin(record)})
    directors = sorted({record_director(record) for record in group_records if record_director(record)})

    row: dict[str, Any] = {
        "group_key": group_key,
        "group_type": group_type,
        "readable_name": readable_name,
        "bins": bins,
        "directors": directors,
        "target_user_id": None,
        "target_user_name": "NO_AVAILABLE_MANAGER_BELOW_LIMITS",
        "reason": reason,
        "reason_debug": reason_debug,
        "owners_before": owners_before,
        "owners_before_names": [user_name(owner) for owner in owners_before],
        "is_split_before": len(set(owners_before)) > 1,
        "company_count": len(company_records),
        "deal_count": len(deal_records),
        "actions": [],
        "errors": [],
    }

    for record in sorted(group_records, key=lambda item: (safe_str(item.get("entity_type")), int(item.get("entity_id") or 0))):
        old_owner = record.get("owner_id")
        row["actions"].append(
            {
                "entity_type": record.get("entity_type"),
                "entity_id": record.get("entity_id"),
                "title": record.get("title"),
                "company_id": record.get("company_id"),
                "closed": bool(record.get("closed")),
                "old_assigned_by_id": old_owner,
                "old_assigned_by_name": user_name(old_owner),
                "new_assigned_by_id": None,
                "new_assigned_by_name": "NO_AVAILABLE_MANAGER_BELOW_LIMITS",
                "action": "skip_no_available_manager_below_limits",
                "error": None,
            }
        )

    return row


def apply_group(
    client: BitrixClient,
    group_key: str,
    group_type: str,
    readable_name: str,
    group_records: list[dict[str, Any]],
    target_user_id: int,
    reason: str,
    reason_debug: dict[str, Any],
    dry_run: bool,
    include_closed_deals: bool,
    sync_contacts: bool,
) -> dict[str, Any]:
    company_records = {safe_str(record.get("company_id")): record for record in group_records if record.get("entity_type") == "company" and safe_str(record.get("company_id"))}
    deal_records = [record for record in group_records if record.get("entity_type") == "deal"]

    owners_before = sorted({record.get("owner_id") for record in group_records if isinstance(record.get("owner_id"), int)})
    bins = sorted({record_bin(record) for record in group_records if record_bin(record)})
    directors = sorted({record_director(record) for record in group_records if record_director(record)})

    row: dict[str, Any] = {
        "group_key": group_key,
        "group_type": group_type,
        "readable_name": readable_name,
        "bins": bins,
        "directors": directors,
        "target_user_id": target_user_id,
        "target_user_name": user_name(target_user_id),
        "reason": reason,
        "reason_debug": reason_debug,
        "owners_before": owners_before,
        "owners_before_names": [user_name(owner) for owner in owners_before],
        "is_split_before": len(set(owners_before)) > 1,
        "company_count": len(company_records),
        "deal_count": len(deal_records),
        "actions": [],
        "errors": [],
    }

    # Repair companies.
    for company_id, record in sorted(company_records.items(), key=lambda item: int(item[0])):
        old_owner = record.get("owner_id")
        action = {
            "entity_type": "company",
            "entity_id": company_id,
            "title": record.get("title"),
            "old_assigned_by_id": old_owner,
            "old_assigned_by_name": user_name(old_owner),
            "new_assigned_by_id": target_user_id,
            "new_assigned_by_name": user_name(target_user_id),
            "action": "skip_already_target" if old_owner == target_user_id else None,
            "error": None,
        }
        if old_owner != target_user_id:
            if dry_run:
                action["action"] = "dry_run_update_company_responsible"
            else:
                try:
                    update_company_owner(client, company_id, target_user_id)
                    action["action"] = "updated_company_responsible"
                except Exception as exc:  # noqa: BLE001
                    action["action"] = "error"
                    action["error"] = str(exc)
                    row["errors"].append(action)
        row["actions"].append(action)

    # Repair deals.
    for record in sorted(deal_records, key=lambda item: int(item.get("entity_id") or 0)):
        deal_id = safe_str(record.get("entity_id"))
        old_owner = record.get("owner_id")
        closed = bool(record.get("closed"))
        action = {
            "entity_type": "deal",
            "entity_id": deal_id,
            "title": record.get("title"),
            "company_id": record.get("company_id"),
            "closed": closed,
            "old_assigned_by_id": old_owner,
            "old_assigned_by_name": user_name(old_owner),
            "new_assigned_by_id": target_user_id,
            "new_assigned_by_name": user_name(target_user_id),
            "action": None,
            "error": None,
        }
        if closed and not include_closed_deals:
            action["action"] = "skip_closed_deal"
        elif old_owner == target_user_id:
            action["action"] = "skip_already_target"
        elif dry_run:
            action["action"] = "dry_run_update_deal_responsible"
        else:
            try:
                update_deal_owner(client, deal_id, target_user_id)
                action["action"] = "updated_deal_responsible"
            except Exception as exc:  # noqa: BLE001
                action["action"] = "error"
                action["error"] = str(exc)
                row["errors"].append(action)
        row["actions"].append(action)

    if sync_contacts:
        for contact_id in sorted(collect_group_contacts(client, group_records), key=lambda x: int(x)):
            old_owner: int | None = None
            title = ""
            try:
                contact = get_contact(client, contact_id)
                if contact:
                    old_owner = owner_id(contact)
                    title = " ".join(
                        part for part in [safe_str(contact.get("LAST_NAME")), safe_str(contact.get("NAME")), safe_str(contact.get("SECOND_NAME"))] if part
                    ).strip()
            except Exception as exc:  # noqa: BLE001
                row["errors"].append({"entity_type": "contact", "entity_id": contact_id, "action": "get_contact_error", "error": str(exc)})
                continue

            action = {
                "entity_type": "contact",
                "entity_id": contact_id,
                "title": title,
                "old_assigned_by_id": old_owner,
                "old_assigned_by_name": user_name(old_owner),
                "new_assigned_by_id": target_user_id,
                "new_assigned_by_name": user_name(target_user_id),
                "action": "skip_already_target" if old_owner == target_user_id else None,
                "error": None,
            }
            if old_owner != target_user_id:
                if dry_run:
                    action["action"] = "dry_run_update_contact_responsible"
                else:
                    try:
                        update_contact_owner(client, contact_id, target_user_id)
                        action["action"] = "updated_contact_responsible"
                    except Exception as exc:  # noqa: BLE001
                        action["action"] = "error"
                        action["error"] = str(exc)
                        row["errors"].append(action)
            row["actions"].append(action)

    return row


def action_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for action in row.get("actions", []):
            counter[safe_str(action.get("action"))] += 1
    return dict(counter)


def write_csv(rows: list[dict[str, Any]], csv_path: str) -> None:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "group_key",
                "group_type",
                "readable_name",
                "bins",
                "directors",
                "owners_before_names",
                "target_user_name",
                "reason",
                "is_split_before",
                "company_count",
                "deal_count",
                "actions_to_change",
                "errors_count",
            ],
        )
        writer.writeheader()
        for row in rows:
            actions_to_change = sum(
                1
                for action in row.get("actions", [])
                if safe_str(action.get("action")).startswith(("dry_run_update", "updated_"))
            )
            writer.writerow(
                {
                    "group_key": row.get("group_key"),
                    "group_type": row.get("group_type"),
                    "readable_name": row.get("readable_name"),
                    "bins": ", ".join(row.get("bins", [])),
                    "directors": ", ".join(row.get("directors", [])),
                    "owners_before_names": ", ".join(row.get("owners_before_names", [])),
                    "target_user_name": row.get("target_user_name"),
                    "reason": row.get("reason"),
                    "is_split_before": row.get("is_split_before"),
                    "company_count": row.get("company_count"),
                    "deal_count": row.get("deal_count"),
                    "actions_to_change": actions_to_change,
                    "errors_count": len(row.get("errors", [])),
                }
            )


def should_process_group(
    *,
    repair_scope: str,
    needs_change: bool,
    is_split: bool,
    has_source_owner: bool,
    is_hard: bool,
) -> bool:
    if repair_scope == "split_only":
        return is_split
    if repair_scope == "split_and_hard":
        return is_split or (is_hard and needs_change)
    if repair_scope == "hard_bin_only":
        return is_hard and needs_change
    if repair_scope == "all_actions":
        return needs_change or is_split or has_source_owner or is_hard
    raise ValueError(f"Unsupported repair_scope: {repair_scope}")


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")

    source_ids = parse_id_set(args.source_responsible_ids)
    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)

    print("Fetching e-Qazyna deals...")
    deals = list_all_eqazyna_deals(client)
    print(f"Deals found: {len(deals)}")

    print("Fetching companies and requisites...")
    companies = build_company_index(client, deals)
    print(f"Companies found/referenced: {len(companies)}")

    records, companies, _company_deals = make_records(deals, companies)
    groups, meta = build_groups(records)
    load = initial_load(companies)
    active_deal_load = initial_active_deal_load(records)
    director_hints = build_director_hints(records, load, source_ids)

    print(f"Records: {len(records)}")
    print(f"Groups: {len(groups)}")
    print(f"Hard BINs configured: {len(HARD_BIN_OWNERS)}")
    print(f"Dry run: {args.dry_run}")
    print(f"Repair scope: {args.repair_scope}")
    print(f"Duplicate hard BIN policy: {args.duplicate_hard_bin_policy}")
    print(f"Limit per manager, companies: {args.limit_per_manager_companies}")
    print(f"Limit per manager, active deals: {args.limit_per_manager_active_deals}")
    print(f"Source responsible IDs: {sorted(source_ids)}")
    print("No batch limit: true")

    rows: list[dict[str, Any]] = []
    skipped_clean_groups = 0
    skipped_duplicate_hard_bin_groups = 0

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            1 if meta[item[0]]["group_type"] == "hard_bin" else 0,
            max((int(record.get("entity_id") or 0) for record in item[1] if record.get("entity_type") == "deal"), default=0),
            item[0],
        ),
        reverse=True,
    )

    for group_key, group_records in ordered_groups:
        group_type = meta[group_key]["group_type"]
        readable_name = meta[group_key]["readable_name"]

        group_bins = {record_bin(record) for record in group_records if record_bin(record)}
        duplicate_hard_conflicts = duplicate_hard_owner_map(group_bins)
        if (
            args.duplicate_hard_bin_policy == "skip"
            and group_type == "hard_bin"
            and duplicate_hard_conflicts
        ):
            rows.append(
                skipped_duplicate_hard_bin_row(
                    group_key=group_key,
                    group_type=group_type,
                    readable_name=readable_name,
                    group_records=group_records,
                    conflicts=duplicate_hard_conflicts,
                )
            )
            skipped_duplicate_hard_bin_groups += 1
            continue

        target, reason, debug = choose_target(
            group_records,
            load,
            active_deal_load,
            source_ids,
            director_hints,
            args.limit_per_manager_companies,
            args.limit_per_manager_active_deals,
        )

        if target is None:
            relevant_records = [
                record
                for record in group_records
                if record.get("entity_type") == "company" or args.include_closed_deals or not record.get("closed")
            ]
            owners = [record.get("owner_id") for record in relevant_records if isinstance(record.get("owner_id"), int)]
            is_split = len(set(owners)) > 1
            has_source_owner = any(owner in source_ids for owner in owners)
            is_hard = group_type == "hard_bin"
            if should_process_group(
                repair_scope=args.repair_scope,
                needs_change=has_source_owner,
                is_split=is_split,
                has_source_owner=has_source_owner,
                is_hard=is_hard,
            ):
                row = skipped_no_available_manager_row(
                    group_key=group_key,
                    group_type=group_type,
                    readable_name=readable_name,
                    group_records=group_records,
                    reason=reason,
                    reason_debug=debug,
                )
                rows.append(row)
            else:
                skipped_clean_groups += 1
            continue

        relevant_records = [
            record
            for record in group_records
            if record.get("entity_type") == "company" or args.include_closed_deals or not record.get("closed")
        ]
        owners = [record.get("owner_id") for record in relevant_records if isinstance(record.get("owner_id"), int)]
        needs_change = any(owner != target for owner in owners)
        is_split = len(set(owners)) > 1
        has_source_owner = any(owner in source_ids for owner in owners)
        is_hard = group_type == "hard_bin"

        if not should_process_group(
            repair_scope=args.repair_scope,
            needs_change=needs_change,
            is_split=is_split,
            has_source_owner=has_source_owner,
            is_hard=is_hard,
        ):
            skipped_clean_groups += 1
            continue

        row = apply_group(
            client=client,
            group_key=group_key,
            group_type=group_type,
            readable_name=readable_name,
            group_records=group_records,
            target_user_id=target,
            reason=reason,
            reason_debug=debug,
            dry_run=args.dry_run,
            include_closed_deals=args.include_closed_deals,
            sync_contacts=args.sync_contacts,
        )
        rows.append(row)

        # Planned load update keeps later lowest-load choices realistic within the same run.
        company_ids = {safe_str(record.get("company_id")) for record in group_records if safe_str(record.get("company_id")) not in {"", "0"}}
        for company_id in company_ids:
            old = owner_id(companies.get(company_id, {}))
            if old == target:
                continue
            if old in load:
                load[old] = max(0, load[old] - 1)
            if target in load:
                load[target] += 1

        for record in group_records:
            if record.get("entity_type") != "deal" or record.get("closed"):
                continue
            old = record.get("owner_id")
            if old == target:
                continue
            if old in active_deal_load:
                active_deal_load[old] = max(0, active_deal_load[old] - 1)
            if target in active_deal_load:
                active_deal_load[target] += 1

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "include_closed_deals": args.include_closed_deals,
        "sync_contacts": args.sync_contacts,
        "repair_scope": args.repair_scope,
        "duplicate_hard_bin_policy": args.duplicate_hard_bin_policy,
        "limit_per_manager_companies": args.limit_per_manager_companies,
        "limit_per_manager_active_deals": args.limit_per_manager_active_deals,
        "source_responsible_ids": sorted(source_ids),
        "source_responsible_names": [user_name(user_id) for user_id in sorted(source_ids)],
        "allowed_user_ids": ALLOWED_USER_IDS,
        "allowed_users": {str(user_id): user_name(user_id) for user_id in ALLOWED_USER_IDS},
        "hard_bin_count": len(HARD_BIN_OWNERS),
        "total_deals": len(deals),
        "total_companies": len(companies),
        "total_records": len(records),
        "total_groups": len(groups),
        "groups_with_actions_or_hard_check": len(rows),
        "skipped_clean_groups": skipped_clean_groups,
        "skipped_duplicate_hard_bin_groups": skipped_duplicate_hard_bin_groups,
        "action_counts": action_counts(rows),
        "final_planned_client_load": {
            str(user_id): {"user_name": user_name(user_id), "client_load": count}
            for user_id, count in sorted(load.items())
        },
        "final_planned_active_deal_load": {
            str(user_id): {"user_name": user_name(user_id), "active_deal_load": count}
            for user_id, count in sorted(active_deal_load.items())
        },
        "distribution_mode": f"audit_all_eqazyna_deals_no_limit_scope_{args.repair_scope}_duplicate_hard_bin_policy_{args.duplicate_hard_bin_policy}_hard_bin_absolute_then_director_then_existing_owner_then_below_limits_lowest_active_deal_load",
    }

    output = {"summary": summary, "groups": rows}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(rows, args.csv_out)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON log: {out_path}")
    print(f"CSV summary: {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
