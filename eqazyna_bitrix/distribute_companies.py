from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .settings import Settings


COMPANY_ORIGINATOR_ID = "EQAZYNA"
SOURCE_RESPONSIBLE_ID = 36
# SOURCE_RESPONSIBLE_IDS is loaded below from managers.yml.


# ---------------------------------------------------------------------
# ЕДИНЫЙ СПРАВОЧНИК МЕНЕДЖЕРОВ
# ---------------------------------------------------------------------
# Менеджеры, их ФИО и филиалы теперь хранятся в eqazyna_bitrix/config/managers.yml.
# Добавляешь нового менеджера туда — pipeline, audit-repair и manual-fix
# подхватывают его автоматически через эти константы.

from .manager_config import load_manager_config
from .config.assignment import load_hard_bin_owners, load_hard_bin_owners_raw

_MANAGER_CONFIG = load_manager_config()

ALLOWED_USER_IDS = _MANAGER_CONFIG.allowed_user_ids
USER_NAMES = _MANAGER_CONFIG.user_names
USER_BRANCHES = _MANAGER_CONFIG.branch_by_user_id
USER_BRANCH_IDS = _MANAGER_CONFIG.branch_id_by_user_id

# source_responsible_ids из YAML пока держим синхронно с историческими
# константами 36/44, чтобы старые workflow продолжали работать без сюрпризов.
SOURCE_RESPONSIBLE_IDS = _MANAGER_CONFIG.source_responsible_ids

# ---------------------------------------------------------------------
# ЖЁСТКОЕ ЗАКРЕПЛЕНИЕ БИНов
# ---------------------------------------------------------------------
# Список хранится в eqazyna_bitrix/config/hard_bins.yml.
# В Python оставлены только готовые константы для обратной совместимости:
# audit_repair_deal_packages.py, pipeline.py и тесты импортируют их отсюда.

HARD_BIN_OWNERS_RAW = load_hard_bin_owners_raw()
HARD_BIN_OWNERS = load_hard_bin_owners()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize and distribute e-Qazyna client packages: companies + active deals"
    )

    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--out", default=None, help="Output JSON log path")
    parser.add_argument(
        "--source-responsible-id",
        type=int,
        default=SOURCE_RESPONSIBLE_ID,
        help="Backward-compatible single source responsible ID",
    )
    parser.add_argument(
        "--source-responsible-ids",
        default=",".join(str(user_id) for user_id in SOURCE_RESPONSIBLE_IDS),
        help="Comma-separated source responsible IDs to redistribute, for example: 36,44",
    )
    parser.add_argument("--limit-per-manager", type=int, default=15)
    parser.add_argument(
        "--limit-per-manager-active-deals",
        type=int,
        default=80,
        help=(
            "Soft limit of active e-Qazyna deals per manager. "
            "Random/new packages are assigned only to managers below this limit before assignment. "
            "0 = ignore active-deal limit."
        ),
    )
    parser.add_argument(
        "--max-new-clients",
        type=int,
        default=18,
        help="Soft max changed company/client cards per run for non-hard packages. 0 = no batch limit",
    )
    parser.add_argument("--seed", type=int, default=None)

    return parser.parse_args()



def _parse_source_responsible_ids(raw: Any, fallback_id: int) -> set[int]:
    values: set[int] = set()

    if raw is not None:
        for part in str(raw).split(","):
            part = part.strip()

            if not part:
                continue

            try:
                values.add(int(part))
            except ValueError as exc:
                raise SystemExit(
                    f"Invalid source responsible ID in --source-responsible-ids: {part!r}"
                ) from exc

    if not values and fallback_id is not None:
        values.add(int(fallback_id))

    return values


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _user_name(user_id: int | None) -> str:
    if user_id is None:
        return "None"
    return USER_NAMES.get(user_id, f"User {user_id}")


def _normalize_text(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(
        r"[^\w\sа-яА-Яa-zA-Z0-9әғқңөұүһіӘҒҚҢӨҰҮҺІ]",
        " ",
        value,
        flags=re.UNICODE,
    )
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalize_bin(raw: Any) -> str:
    value = _safe_str(raw).strip()

    if not value:
        return ""

    if "e" in value.lower():
        return ""

    digits = re.sub(r"\D", "", value)

    if len(digits) != 12:
        return ""

    return digits


def _extract_director_from_comments(comments: str) -> str:
    if not comments:
        return ""

    patterns = [
        r"(?:первый\s+руководитель|руководитель|директор)\s*[:\-]\s*(.+)",
        r"(?:фио\s+руководителя)\s*[:\-]\s*(.+)",
    ]

    invalid_values = {
        "не найден",
        "не найдена",
        "не найдено",
        "не указан",
        "не указана",
        "не указано",
        "нет",
        "нет данных",
        "отсутствует",
        "n a",
        "na",
        "n/a",
        "-",
        "—",
    }

    for pattern in patterns:
        match = re.search(pattern, comments, flags=re.IGNORECASE)
        if not match:
            continue

        director = match.group(1).strip()
        director = director.split("\n")[0].strip()
        director = re.sub(r"\s{2,}", " ", director)

        normalized = _normalize_text(director)

        if not normalized:
            return ""

        if normalized in invalid_values:
            return ""

        return director[:255]

    return ""


def _company_bin(company: dict[str, Any]) -> str:
    return _normalize_bin(company.get("ORIGIN_ID"))


def _company_title(company: dict[str, Any]) -> str:
    return _safe_str(company.get("TITLE") or "Компания без названия")


def _company_owner_id(company: dict[str, Any]) -> int | None:
    return _to_int(company.get("ASSIGNED_BY_ID"))


def _company_director(company: dict[str, Any]) -> str:
    return _extract_director_from_comments(_safe_str(company.get("COMMENTS")))


def _company_group_key(company: dict[str, Any]) -> tuple[str, str, str]:
    director = _company_director(company)

    if director:
        normalized = _normalize_text(director)
        return f"director|{normalized}", "director", director

    bin_value = _company_bin(company) or f"company-{company.get('ID')}"
    return f"company|{bin_value}", "company", _company_title(company)


def _is_active_deal(deal: dict[str, Any]) -> bool:
    return _safe_str(deal.get("CLOSED")).upper() != "Y"


def _list_eqazyna_companies(client: BitrixClient) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {
                "ORIGINATOR_ID": COMPANY_ORIGINATOR_ID,
            },
            "select": [
                "ID",
                "TITLE",
                "ASSIGNED_BY_ID",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
                "COMMENTS",
            ],
        },
    )


def _list_company_deals(client: BitrixClient, company_id: str) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": {
                "COMPANY_ID": int(company_id),
            },
            "select": [
                "ID",
                "TITLE",
                "STAGE_ID",
                "CLOSED",
                "ASSIGNED_BY_ID",
                "COMPANY_ID",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
            ],
        },
    )


def _update_company_responsible(client: BitrixClient, company_id: str, user_id: int) -> None:
    client.call(
        "crm.company.update",
        {
            "id": int(company_id),
            "fields": {
                "ASSIGNED_BY_ID": user_id,
            },
            "params": {
                "REGISTER_SONET_EVENT": "N",
            },
        },
    )


def _update_deal_responsible(client: BitrixClient, deal_id: str, user_id: int) -> None:
    client.call(
        "crm.deal.update",
        {
            "id": int(deal_id),
            "fields": {
                "ASSIGNED_BY_ID": user_id,
            },
            "params": {
                "REGISTER_SONET_EVENT": "N",
            },
        },
    )


def _initial_client_load(companies: list[dict[str, Any]]) -> dict[int, int]:
    load = {user_id: 0 for user_id in ALLOWED_USER_IDS}

    for company in companies:
        owner_id = _company_owner_id(company)

        if owner_id in load:
            load[owner_id] += 1

    return load


def _deal_owner_id(deal: dict[str, Any]) -> int | None:
    return _to_int(deal.get("ASSIGNED_BY_ID"))


def _initial_active_deal_load(deal_cache: dict[str, list[dict[str, Any]]]) -> dict[int, int]:
    load = {user_id: 0 for user_id in ALLOWED_USER_IDS}

    for deals in deal_cache.values():
        for deal in deals:
            if not _is_active_deal(deal):
                continue

            owner_id = _deal_owner_id(deal)

            if owner_id in load:
                load[owner_id] += 1

    return load


def _group_active_deal_inbound_count(
    group_companies: list[dict[str, Any]],
    deal_cache: dict[str, list[dict[str, Any]]],
    target_user_id: int,
) -> int:
    count = 0

    for company in group_companies:
        company_id = str(company.get("ID"))

        for deal in deal_cache.get(company_id, []):
            if not _is_active_deal(deal):
                continue

            if _deal_owner_id(deal) != target_user_id:
                count += 1

    return count


def _manager_under_limits(
    *,
    user_id: int,
    client_load: dict[int, int],
    active_deal_load: dict[int, int],
    limit_per_manager: int,
    limit_per_manager_active_deals: int,
) -> bool:
    if limit_per_manager and limit_per_manager > 0:
        if client_load.get(user_id, 0) >= limit_per_manager:
            return False

    if limit_per_manager_active_deals and limit_per_manager_active_deals > 0:
        if active_deal_load.get(user_id, 0) >= limit_per_manager_active_deals:
            return False

    return True


def _choose_random_available(
    client_load: dict[int, int],
    active_deal_load: dict[int, int],
    limit_per_manager: int,
    limit_per_manager_active_deals: int,
    group_companies: list[dict[str, Any]],
    deal_cache: dict[str, list[dict[str, Any]]],
) -> tuple[int | None, bool, dict[str, Any]]:
    eligible: list[int] = []
    blocked: dict[int, dict[str, int]] = {}

    for user_id in ALLOWED_USER_IDS:
        current_clients = client_load.get(user_id, 0)
        current_active_deals = active_deal_load.get(user_id, 0)

        if not _manager_under_limits(
            user_id=user_id,
            client_load=client_load,
            active_deal_load=active_deal_load,
            limit_per_manager=limit_per_manager,
            limit_per_manager_active_deals=limit_per_manager_active_deals,
        ):
            blocked[user_id] = {
                "client_load": current_clients,
                "active_deal_load": current_active_deals,
            }
            continue

        eligible.append(user_id)

    if not eligible:
        return None, False, {
            "blocked_managers": {str(user_id): data for user_id, data in sorted(blocked.items())},
            "client_limit": limit_per_manager,
            "active_deal_limit": limit_per_manager_active_deals,
        }

    min_active_deals = min(active_deal_load.get(user_id, 0) for user_id in eligible)
    active_deal_candidates = [
        user_id
        for user_id in eligible
        if active_deal_load.get(user_id, 0) == min_active_deals
    ]

    min_clients = min(client_load.get(user_id, 0) for user_id in active_deal_candidates)
    client_candidates = [
        user_id
        for user_id in active_deal_candidates
        if client_load.get(user_id, 0) == min_clients
    ]

    target_user_id = random.choice(sorted(client_candidates))

    company_inbound = sum(
        1
        for company in group_companies
        if _company_owner_id(company) != target_user_id
    )
    active_deal_inbound = _group_active_deal_inbound_count(group_companies, deal_cache, target_user_id)

    client_after = client_load.get(target_user_id, 0) + company_inbound
    active_deal_after = active_deal_load.get(target_user_id, 0) + active_deal_inbound

    soft_limit_expanded = (
        (limit_per_manager and limit_per_manager > 0 and client_after > limit_per_manager)
        or (
            limit_per_manager_active_deals
            and limit_per_manager_active_deals > 0
            and active_deal_after > limit_per_manager_active_deals
        )
    )

    return target_user_id, bool(soft_limit_expanded), {
        "eligible_manager_ids": sorted(eligible),
        "selected_by": "lowest_active_deal_load_then_lowest_client_load_then_random",
        "client_load_before": client_load.get(target_user_id, 0),
        "client_load_after": client_after,
        "active_deal_load_before": active_deal_load.get(target_user_id, 0),
        "active_deal_load_after": active_deal_after,
        "client_limit": limit_per_manager,
        "active_deal_limit": limit_per_manager_active_deals,
    }


def _choose_existing_allowed_owner(
    group_companies: list[dict[str, Any]],
    source_responsible_ids: set[int],
    load: dict[int, int],
) -> int | None:
    counts: Counter[int] = Counter()

    for company in group_companies:
        owner_id = _company_owner_id(company)

        if owner_id is None:
            continue

        if owner_id in source_responsible_ids:
            continue

        if owner_id not in ALLOWED_USER_IDS:
            continue

        counts[owner_id] += 1

    if not counts:
        return None

    max_count = max(counts.values())

    candidates = [
        user_id
        for user_id, count in counts.items()
        if count == max_count
    ]

    candidates.sort(key=lambda user_id: (load.get(user_id, 0), user_id))

    return candidates[0]


def _choose_hard_owner_for_group(
    group_companies: list[dict[str, Any]],
    load: dict[int, int],
) -> dict[str, Any] | None:
    votes: Counter[int] = Counter()
    current_owner_votes: Counter[int] = Counter()
    matched_bins: list[dict[str, Any]] = []

    for company in group_companies:
        bin_value = _company_bin(company)

        if not bin_value:
            continue

        owner_ids = HARD_BIN_OWNERS.get(bin_value, [])

        if not owner_ids:
            continue

        current_owner_id = _company_owner_id(company)

        matched_bins.append(
            {
                "company_id": company.get("ID"),
                "company_title": company.get("TITLE"),
                "bin": bin_value,
                "hard_owner_ids": owner_ids,
                "hard_owner_names": [_user_name(user_id) for user_id in owner_ids],
                "current_owner_id": current_owner_id,
                "current_owner_name": _user_name(current_owner_id),
            }
        )

        for owner_id in owner_ids:
            if owner_id in ALLOWED_USER_IDS:
                votes[owner_id] += 1

            if current_owner_id == owner_id:
                current_owner_votes[owner_id] += 1

    if not votes:
        return None

    max_votes = max(votes.values())

    candidates = [
        user_id
        for user_id, count in votes.items()
        if count == max_votes
    ]

    if len(candidates) == 1:
        target_user_id = candidates[0]
        reason = "hard_bin_owner"
    else:
        max_current_votes = max(
            current_owner_votes.get(user_id, 0)
            for user_id in candidates
        )

        current_owner_candidates = [
            user_id
            for user_id in candidates
            if current_owner_votes.get(user_id, 0) == max_current_votes
        ]

        if max_current_votes > 0 and len(current_owner_candidates) == 1:
            target_user_id = current_owner_candidates[0]
            reason = "hard_bin_owner_tie_resolved_by_current_owner"
        else:
            current_owner_candidates.sort(key=lambda user_id: (load.get(user_id, 0), user_id))
            target_user_id = current_owner_candidates[0]
            reason = "hard_bin_owner_tie_resolved_by_lowest_load"

    return {
        "target_user_id": target_user_id,
        "target_user_name": _user_name(target_user_id),
        "reason": reason,
        "hard_votes": {
            str(user_id): {
                "user_name": _user_name(user_id),
                "votes": count,
            }
            for user_id, count in sorted(votes.items())
        },
        "matched_bins": matched_bins,
    }


def _company_short(company: dict[str, Any]) -> dict[str, Any]:
    owner_id = _company_owner_id(company)

    return {
        "company_id": company.get("ID"),
        "company_title": company.get("TITLE"),
        "bin": _company_bin(company),
        "assigned_by_id": owner_id,
        "assigned_by_name": _user_name(owner_id),
    }


def _strict_target_for_company(company: dict[str, Any], package_target_user_id: int) -> tuple[int, str | None]:
    """Hard BIN ownership is absolute for that BIN.

    A director package may be normalized to one manager, but if a specific BIN
    is hard-fixed to another manager, that exact company/deals must not be
    moved away from the hard owner. This is the iron rule above package repair.
    """
    bin_value = _company_bin(company)
    hard_owner_ids = [user_id for user_id in HARD_BIN_OWNERS.get(bin_value, []) if user_id in ALLOWED_USER_IDS]

    if not hard_owner_ids:
        return package_target_user_id, None

    if package_target_user_id in hard_owner_ids:
        return package_target_user_id, "package_target_matches_hard_bin_owner"

    current_owner_id = _company_owner_id(company)
    if current_owner_id in hard_owner_ids:
        return current_owner_id, "hard_bin_exact_owner_preserved"

    return hard_owner_ids[0], "hard_bin_exact_owner_override"


def _sync_company_and_deals(
    client: BitrixClient,
    company: dict[str, Any],
    target_user_id: int,
    dry_run: bool,
    deals: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int, int]:
    company_id = str(company.get("ID"))
    old_company_owner_id = _company_owner_id(company)

    company_changes = 0
    deal_changes = 0

    sync: dict[str, Any] = {
        "company": None,
        "deals": [],
        "errors": [],
    }

    company_row = {
        "company_id": company_id,
        "company_title": company.get("TITLE"),
        "bin": _company_bin(company),
        "old_assigned_by_id": old_company_owner_id,
        "old_assigned_by_name": _user_name(old_company_owner_id),
        "new_assigned_by_id": target_user_id,
        "new_assigned_by_name": _user_name(target_user_id),
        "action": None,
        "error": None,
    }

    if old_company_owner_id == target_user_id:
        company_row["action"] = "skip_company_already_target"
    elif dry_run:
        company_row["action"] = "dry_run_update_company_responsible"
        company_changes += 1
    else:
        try:
            _update_company_responsible(client, company_id, target_user_id)
            company_row["action"] = "updated_company_responsible"
            company_changes += 1
        except Exception as exc:
            company_row["action"] = "error"
            company_row["error"] = str(exc)
            sync["errors"].append(company_row)

    sync["company"] = company_row

    if deals is None:
        try:
            deals = _list_company_deals(client, company_id)
        except Exception as exc:
            sync["errors"].append(
                {
                    "company_id": company_id,
                    "action": "list_deals_error",
                    "error": str(exc),
                }
            )
            deals = []

    for deal in deals:
        deal_id = str(deal.get("ID"))
        old_deal_owner_id = _to_int(deal.get("ASSIGNED_BY_ID"))

        deal_row = {
            "company_id": company_id,
            "deal_id": deal_id,
            "deal_title": deal.get("TITLE"),
            "stage_id": deal.get("STAGE_ID"),
            "closed": deal.get("CLOSED"),
            "old_assigned_by_id": old_deal_owner_id,
            "old_assigned_by_name": _user_name(old_deal_owner_id),
            "new_assigned_by_id": target_user_id,
            "new_assigned_by_name": _user_name(target_user_id),
            "action": None,
            "error": None,
        }

        if not _is_active_deal(deal):
            deal_row["action"] = "skip_closed_deal"
            sync["deals"].append(deal_row)
            continue

        if old_deal_owner_id == target_user_id:
            deal_row["action"] = "skip_deal_already_target"
            sync["deals"].append(deal_row)
            continue

        if dry_run:
            deal_row["action"] = "dry_run_update_deal_responsible"
            deal_changes += 1
        else:
            try:
                _update_deal_responsible(client, deal_id, target_user_id)
                deal_row["action"] = "updated_deal_responsible"
                deal_changes += 1
            except Exception as exc:
                deal_row["action"] = "error"
                deal_row["error"] = str(exc)
                sync["errors"].append(deal_row)

        sync["deals"].append(deal_row)

    return sync, company_changes, deal_changes


def _group_has_hard_bin(group_companies: list[dict[str, Any]]) -> bool:
    return any(_company_bin(company) in HARD_BIN_OWNERS for company in group_companies)


def _group_latest_active_deal_id(
    group_companies: list[dict[str, Any]],
    deal_cache: dict[str, list[dict[str, Any]]],
) -> int:
    latest = 0
    for company in group_companies:
        company_id = str(company.get("ID"))
        for deal in deal_cache.get(company_id, []):
            if not _is_active_deal(deal):
                continue
            deal_id = _to_int(deal.get("ID")) or 0
            latest = max(latest, deal_id)
    return latest


def _group_sort_key(
    item: tuple[str, list[dict[str, Any]]],
    source_responsible_ids: set[int],
    deal_cache: dict[str, list[dict[str, Any]]],
) -> tuple[int, int, int, int]:
    _group_key, group_companies = item
    has_hard = 1 if _group_has_hard_bin(group_companies) else 0
    source_count = sum(1 for company in group_companies if _company_owner_id(company) in source_responsible_ids)
    latest_deal_id = _group_latest_active_deal_id(group_companies, deal_cache)
    latest_company_id = max((_to_int(company.get("ID")) or 0) for company in group_companies)
    return (has_hard, source_count, latest_deal_id, latest_company_id)


def _apply_planned_load_change(
    load: dict[int, int],
    group_companies: list[dict[str, Any]],
    target_user_id: int,
) -> None:
    for company in group_companies:
        old_owner_id = _company_owner_id(company)

        if old_owner_id == target_user_id:
            continue

        if old_owner_id in load:
            load[old_owner_id] = max(0, load[old_owner_id] - 1)

        if target_user_id in load:
            load[target_user_id] += 1


def _apply_planned_active_deal_load_change(
    active_deal_load: dict[int, int],
    group_companies: list[dict[str, Any]],
    deal_cache: dict[str, list[dict[str, Any]]],
    target_user_id: int,
) -> None:
    for company in group_companies:
        company_id = str(company.get("ID"))

        for deal in deal_cache.get(company_id, []):
            if not _is_active_deal(deal):
                continue

            old_owner_id = _deal_owner_id(deal)

            if old_owner_id == target_user_id:
                continue

            if old_owner_id in active_deal_load:
                active_deal_load[old_owner_id] = max(0, active_deal_load[old_owner_id] - 1)

            if target_user_id in active_deal_load:
                active_deal_load[target_user_id] += 1


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()

    if args.seed is not None:
        random.seed(args.seed)

    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")

    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)

    companies = _list_eqazyna_companies(client)

    print(f"e-Qazyna companies found: {len(companies)}")
    print(f"Dry run: {args.dry_run}")
    source_responsible_ids = _parse_source_responsible_ids(args.source_responsible_ids, args.source_responsible_id)
    source_responsible_ids_sorted = sorted(source_responsible_ids)

    print(f"Source responsible IDs: {source_responsible_ids_sorted}")
    print(f"Allowed users: {len(ALLOWED_USER_IDS)}")
    print(f"Hard BINs: {len(HARD_BIN_OWNERS)}")
    print(f"Limit per manager, clients: {args.limit_per_manager}")
    print(f"Limit per manager, active deals: {args.limit_per_manager_active_deals}")
    print(f"Max changed clients per run, non-hard only: {args.max_new_clients}")

    load = _initial_client_load(companies)

    print("Initial client load:")
    for user_id, count in sorted(load.items(), key=lambda item: (item[1], item[0])):
        print(f"  {user_id} {_user_name(user_id)}: {count}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_meta: dict[str, dict[str, str]] = {}

    for company in companies:
        group_key, group_type, readable_name = _company_group_key(company)
        groups[group_key].append(company)
        group_meta[group_key] = {
            "group_type": group_type,
            "readable_name": readable_name,
        }

    deal_cache: dict[str, list[dict[str, Any]]] = {}
    for company in companies:
        company_id = str(company.get("ID"))
        try:
            deal_cache[company_id] = _list_company_deals(client, company_id)
        except Exception as exc:  # noqa: BLE001 - distribution must still produce a log
            print(f"WARN: could not list deals for company {company_id}: {exc}")
            deal_cache[company_id] = []

    active_deal_load = _initial_active_deal_load(deal_cache)

    print("Initial active deal load:")
    for user_id, count in sorted(active_deal_load.items(), key=lambda item: (item[1], item[0])):
        print(f"  {user_id} {_user_name(user_id)}: {count}")

    ordered_groups = sorted(
        groups.items(),
        key=lambda item: _group_sort_key(item, source_responsible_ids, deal_cache),
        reverse=True,
    )

    results: list[dict[str, Any]] = []

    changed_clients_planned = 0
    changed_groups_planned = 0
    changed_deals_planned = 0

    hard_changed_clients_planned = 0
    hard_changed_groups_planned = 0
    hard_changed_deals_planned = 0

    action_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()

    summary: dict[str, Any] = {
        "total_companies": len(companies),
        "total_company_groups": len(groups),
        "dry_run": args.dry_run,
        "source_responsible_ids": source_responsible_ids_sorted,
        "source_responsible_names": [
            _user_name(user_id)
            for user_id in source_responsible_ids_sorted
        ],
        "source_responsible_id_legacy": args.source_responsible_id,
        "allowed_user_ids": ALLOWED_USER_IDS,
        "allowed_users": {
            str(user_id): _user_name(user_id)
            for user_id in ALLOWED_USER_IDS
        },
        "hard_bin_count": len(HARD_BIN_OWNERS),
        "hard_bin_owners": {
            bin_value: {
                "owner_ids": owner_ids,
                "owner_names": [_user_name(user_id) for user_id in owner_ids],
            }
            for bin_value, owner_ids in sorted(HARD_BIN_OWNERS.items())
        },
        "limit_per_manager_clients": args.limit_per_manager,
        "limit_per_manager_active_deals": args.limit_per_manager_active_deals,
        "max_changed_clients_non_hard": args.max_new_clients,
        "distribution_mode": "hard_bin_absolute_then_existing_director_or_bin_owner_then_newest_random_package",
        "seed": args.seed,
    }

    for group_key, group_companies in ordered_groups:
        meta = group_meta[group_key]
        group_type = meta["group_type"]
        readable_name = meta["readable_name"]

        all_owner_ids = sorted(
            {
                _company_owner_id(company)
                for company in group_companies
                if _company_owner_id(company) is not None
            }
        )

        source_company_count = sum(
            1
            for company in group_companies
            if _company_owner_id(company) in source_responsible_ids
        )

        allowed_existing_owner_ids = sorted(
            {
                owner_id
                for owner_id in all_owner_ids
                if owner_id in ALLOWED_USER_IDS and owner_id not in source_responsible_ids
            }
        )

        non_allowed_owner_ids = sorted(
            {
                owner_id
                for owner_id in all_owner_ids
                if owner_id not in ALLOWED_USER_IDS and owner_id not in source_responsible_ids
            }
        )

        has_source_companies = source_company_count > 0
        has_split_companies = len(all_owner_ids) > 1

        row: dict[str, Any] = {
            "group_key": group_key,
            "group_type": group_type,
            "readable_name": readable_name,
            "group_company_count": len(group_companies),
            "source_company_count": source_company_count,
            "all_owner_ids": all_owner_ids,
            "all_owner_names": [_user_name(owner_id) for owner_id in all_owner_ids],
            "allowed_existing_owner_ids": allowed_existing_owner_ids,
            "allowed_existing_owner_names": [
                _user_name(owner_id)
                for owner_id in allowed_existing_owner_ids
            ],
            "non_allowed_owner_ids": non_allowed_owner_ids,
            "non_allowed_owner_names": [
                _user_name(owner_id)
                for owner_id in non_allowed_owner_ids
            ],
            "is_hard_package": False,
            "hard_decision": None,
            "target_user_id": None,
            "target_user_name": None,
            "reason": None,
            "action": None,
            "companies": [],
            "sync": [],
            "changed_company_count": 0,
            "changed_deal_count": 0,
            "manager_limit_expanded": False,
            "manager_limit": args.limit_per_manager,
            "manager_active_deal_limit_expanded": False,
            "manager_active_deal_limit": args.limit_per_manager_active_deals,
            "manager_load_before": None,
            "manager_load_after": None,
            "manager_active_deal_load_before": None,
            "manager_active_deal_load_after": None,
            "batch_limit_expanded": False,
            "batch_limit": args.max_new_clients,
            "batch_load_before": changed_clients_planned,
            "batch_load_after": changed_clients_planned,
            "error": None,
        }

        hard_decision = _choose_hard_owner_for_group(
            group_companies=group_companies,
            load=load,
        )

        if hard_decision is not None:
            target_user_id = hard_decision["target_user_id"]
            target_reason = hard_decision["reason"]

            row["is_hard_package"] = True
            row["hard_decision"] = hard_decision

            manager_load_before = load.get(target_user_id, 0)

            inbound_to_target = sum(
                1
                for company in group_companies
                if _company_owner_id(company) != target_user_id
            )

            manager_load_after = manager_load_before + inbound_to_target
            manager_limit_expanded = manager_load_after > args.limit_per_manager
            manager_active_deal_load_before = active_deal_load.get(target_user_id, 0)
            manager_active_deal_load_after = manager_active_deal_load_before + _group_active_deal_inbound_count(
                group_companies,
                deal_cache,
                target_user_id,
            )
            manager_active_deal_limit_expanded = (
                args.limit_per_manager_active_deals > 0
                and manager_active_deal_load_after > args.limit_per_manager_active_deals
            )
            manager_active_deal_load_before = active_deal_load.get(target_user_id, 0)
            manager_active_deal_load_after = manager_active_deal_load_before + _group_active_deal_inbound_count(
                group_companies,
                deal_cache,
                target_user_id,
            )
            manager_active_deal_limit_expanded = (
                args.limit_per_manager_active_deals > 0
                and manager_active_deal_load_after > args.limit_per_manager_active_deals
            )

        else:
            existing_allowed_owner = _choose_existing_allowed_owner(
                group_companies=group_companies,
                source_responsible_ids=source_responsible_ids,
                load=load,
            )

            if existing_allowed_owner is not None:
                target_user_id = existing_allowed_owner

                if has_split_companies:
                    target_reason = "repair_split_package_to_dominant_allowed_owner"
                else:
                    target_reason = "sync_clean_package_to_existing_allowed_owner"

                manager_load_before = load.get(target_user_id, 0)

                inbound_to_target = sum(
                    1
                    for company in group_companies
                    if _company_owner_id(company) != target_user_id
                )

                manager_load_after = manager_load_before + inbound_to_target
                manager_active_deal_load_before = active_deal_load.get(target_user_id, 0)
                manager_active_deal_load_after = manager_active_deal_load_before + _group_active_deal_inbound_count(
                    group_companies,
                    deal_cache,
                    target_user_id,
                )
                manager_active_deal_limit_expanded = (
                    args.limit_per_manager_active_deals > 0
                    and manager_active_deal_load_after > args.limit_per_manager_active_deals
                )
                manager_limit_expanded = manager_load_after > args.limit_per_manager

            elif has_source_companies:
                target_user_id, manager_limit_expanded, random_limit_debug = _choose_random_available(
                    client_load=load,
                    active_deal_load=active_deal_load,
                    limit_per_manager=args.limit_per_manager,
                    limit_per_manager_active_deals=args.limit_per_manager_active_deals,
                    group_companies=group_companies,
                    deal_cache=deal_cache,
                )

                if target_user_id is None:
                    row["action"] = "skip_no_available_managers"
                    row["reason"] = "no_manager_below_client_or_active_deal_limit"
                    row["limit_debug"] = random_limit_debug
                    row["companies"] = [_company_short(company) for company in group_companies]

                    results.append(row)
                    action_counter[row["action"]] += 1
                    reason_counter[row["reason"]] += 1
                    continue

                target_reason = (
                    "random_with_soft_client_or_active_deal_limit_expanded_for_package"
                    if manager_limit_expanded
                    else "random_new_package_below_limits"
                )

                manager_load_before = load.get(target_user_id, 0)

                inbound_to_target = sum(
                    1
                    for company in group_companies
                    if _company_owner_id(company) != target_user_id
                )

                manager_load_after = manager_load_before + inbound_to_target

            else:
                row["action"] = "skip_no_source_and_no_allowed_owner"
                row["reason"] = "package_has_no_source_companies_no_allowed_owner_no_hard_bin"
                row["companies"] = [_company_short(company) for company in group_companies]

                results.append(row)
                action_counter[row["action"]] += 1
                reason_counter[row["reason"]] += 1
                continue

        changed_company_count = sum(
            1
            for company in group_companies
            if _company_owner_id(company) != target_user_id
        )

        # Лимит запуска применяется только к НЕжёстким пакетам.
        # HARD-пакеты идут всегда, даже если лимит уже превышен.
        if not row["is_hard_package"]:
            if changed_company_count > 0 and args.max_new_clients and args.max_new_clients > 0:
                remaining_batch_capacity = args.max_new_clients - changed_clients_planned

                if remaining_batch_capacity <= 0:
                    row["action"] = "skip_batch_limit_reached"
                    row["reason"] = "max_changed_clients_for_this_run_reached"
                    row["target_user_id"] = target_user_id
                    row["target_user_name"] = _user_name(target_user_id)
                    row["companies"] = [_company_short(company) for company in group_companies]

                    results.append(row)
                    action_counter[row["action"]] += 1
                    reason_counter[row["reason"]] += 1
                    continue

                if changed_company_count > remaining_batch_capacity:
                    row["batch_limit_expanded"] = True

                row["batch_load_before"] = changed_clients_planned
                row["batch_load_after"] = changed_clients_planned + changed_company_count
        else:
            if changed_company_count > 0 and args.max_new_clients and args.max_new_clients > 0:
                row["batch_load_before"] = changed_clients_planned
                row["batch_load_after"] = changed_clients_planned + changed_company_count

                if changed_clients_planned + changed_company_count > args.max_new_clients:
                    row["batch_limit_expanded"] = True

        row["target_user_id"] = target_user_id
        row["target_user_name"] = _user_name(target_user_id)
        row["reason"] = target_reason
        row["action"] = "dry_run_normalize_package" if args.dry_run else "normalize_package"

        row["changed_company_count"] = changed_company_count
        row["manager_limit_expanded"] = manager_limit_expanded
        row["manager_active_deal_limit_expanded"] = manager_active_deal_limit_expanded
        row["manager_load_before"] = manager_load_before
        row["manager_load_after"] = manager_load_after
        row["manager_active_deal_load_before"] = manager_active_deal_load_before
        row["manager_active_deal_load_after"] = manager_active_deal_load_after

        package_company_changes = 0
        package_deal_changes = 0

        for company in group_companies:
            company_target_user_id, company_target_override_reason = _strict_target_for_company(company, target_user_id)

            row["companies"].append(
                {
                    "company_id": company.get("ID"),
                    "company_title": company.get("TITLE"),
                    "bin": _company_bin(company),
                    "old_assigned_by_id": _company_owner_id(company),
                    "old_assigned_by_name": _user_name(_company_owner_id(company)),
                    "package_target_user_id": target_user_id,
                    "package_target_user_name": _user_name(target_user_id),
                    "new_assigned_by_id": company_target_user_id,
                    "new_assigned_by_name": _user_name(company_target_user_id),
                    "company_target_override_reason": company_target_override_reason,
                    "hard_bin_owners": HARD_BIN_OWNERS.get(_company_bin(company), []),
                    "hard_bin_owner_names": [
                        _user_name(user_id)
                        for user_id in HARD_BIN_OWNERS.get(_company_bin(company), [])
                    ],
                }
            )

            sync, company_changes, deal_changes = _sync_company_and_deals(
                client=client,
                company=company,
                target_user_id=company_target_user_id,
                dry_run=args.dry_run,
                deals=deal_cache.get(str(company.get("ID"))),
            )

            package_company_changes += company_changes
            package_deal_changes += deal_changes

            row["sync"].append(sync)

        row["changed_company_count"] = package_company_changes
        row["changed_deal_count"] = package_deal_changes

        if package_company_changes > 0:
            _apply_planned_load_change(
                load=load,
                group_companies=group_companies,
                target_user_id=target_user_id,
            )

            changed_clients_planned += package_company_changes
            changed_groups_planned += 1

            if row["is_hard_package"]:
                hard_changed_clients_planned += package_company_changes
                hard_changed_groups_planned += 1

        if package_deal_changes > 0:
            _apply_planned_active_deal_load_change(
                active_deal_load=active_deal_load,
                group_companies=group_companies,
                deal_cache=deal_cache,
                target_user_id=target_user_id,
            )

        changed_deals_planned += package_deal_changes

        if row["is_hard_package"]:
            hard_changed_deals_planned += package_deal_changes

        results.append(row)
        action_counter[row["action"]] += 1
        reason_counter[row["reason"]] += 1

    summary["changed_clients_planned"] = changed_clients_planned
    summary["changed_groups_planned"] = changed_groups_planned
    summary["changed_deals_planned"] = changed_deals_planned

    summary["hard_changed_clients_planned"] = hard_changed_clients_planned
    summary["hard_changed_groups_planned"] = hard_changed_groups_planned
    summary["hard_changed_deals_planned"] = hard_changed_deals_planned

    summary["action_counts"] = dict(action_counter)
    summary["reason_counts"] = dict(reason_counter)

    summary["final_planned_client_load"] = {
        str(user_id): {
            "user_name": _user_name(user_id),
            "client_load": count,
        }
        for user_id, count in sorted(load.items())
    }

    summary["final_planned_active_deal_load"] = {
        str(user_id): {
            "user_name": _user_name(user_id),
            "active_deal_load": count,
        }
        for user_id, count in sorted(active_deal_load.items())
    }

    out = Path(args.out or f"exports/distribute_companies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "summary": summary,
        "results": results,
    }

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"JSON: {out}")
    print("Done")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
