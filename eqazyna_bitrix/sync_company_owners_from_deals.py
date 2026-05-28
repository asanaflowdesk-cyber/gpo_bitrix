from __future__ import annotations

import argparse
import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .config.assignment import load_hard_bin_owners, load_manual_director_owners_raw
from .config.managers import load_manager_config


ORIGINATOR_ID = "EQAZYNA"


def normalize_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_bin(value: Any) -> str:
    digits = normalize_digits(value)
    return digits if len(digits) == 12 else ""


def normalize_name(value: Any) -> str:
    text = str(value or "").replace("Ё", "Е").replace("ё", "е")
    text = re.sub(r"[^0-9A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺһ\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().upper()
    return text


def parse_ids(raw: str) -> set[str]:
    return {part.strip() for part in str(raw or "").split(",") if part.strip()}


def user_name(user_id: Any, names: dict[int, str]) -> str:
    text = str(user_id or "").strip()
    if not text:
        return ""
    try:
        return names.get(int(text), f"#{text}")
    except ValueError:
        return f"#{text}"


def contact_fio(contact: dict[str, Any] | None) -> str:
    if not contact:
        return ""
    parts = [contact.get("LAST_NAME"), contact.get("NAME"), contact.get("SECOND_NAME")]
    return " ".join(str(p or "").strip() for p in parts if str(p or "").strip())


def make_url_hint(entity: str, entity_id: str) -> str:
    # Domain is intentionally not guessed. This is enough to paste after /crm/.
    return f"/crm/{entity}/details/{entity_id}/"


@dataclass
class CompanyInfo:
    id: str
    title: str = ""
    owner_id: str = ""
    owner_name: str = ""
    bins: list[str] = field(default_factory=list)


@dataclass
class DealInfo:
    id: str
    title: str = ""
    company_id: str = ""
    contact_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    stage_id: str = ""
    closed: str = ""
    category_id: str = ""
    origin_id: str = ""


@dataclass
class DirectorGroup:
    contact_id: str
    fio: str
    contact_owner_id: str
    contact_owner_name: str
    deals: list[DealInfo] = field(default_factory=list)
    companies: dict[str, CompanyInfo] = field(default_factory=dict)
    target_owner_id: str = ""
    target_owner_name: str = ""
    target_reason: str = ""
    status: str = ""
    mismatch_count: int = 0
    action_count: int = 0


def load_manual_director_index() -> dict[str, str]:
    raw = load_manual_director_owners_raw()
    index: dict[str, str] = {}
    for user_id, names in raw.items():
        for name in names:
            norm = normalize_name(name)
            if norm:
                index[norm] = str(user_id)
    return index


def list_deals(
    bitrix: BitrixClient,
    *,
    only_eqazyna: bool,
    deal_category_id: str,
    include_closed_deals: bool,
    max_deals: int,
) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {}
    if only_eqazyna:
        flt["ORIGINATOR_ID"] = ORIGINATOR_ID
    if deal_category_id and deal_category_id.lower() != "all":
        flt["CATEGORY_ID"] = int(deal_category_id) if str(deal_category_id).isdigit() else deal_category_id
    if not include_closed_deals:
        flt["CLOSED"] = "N"

    return bitrix.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": [
                "ID",
                "TITLE",
                "COMPANY_ID",
                "CONTACT_ID",
                "ASSIGNED_BY_ID",
                "STAGE_ID",
                "CLOSED",
                "CATEGORY_ID",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
            ],
        },
        limit=max_deals or None,
    )


def get_contact(bitrix: BitrixClient, contact_id: str, cache: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    contact_id = str(contact_id or "").strip()
    if not contact_id:
        return None
    if contact_id not in cache:
        result = bitrix.call("crm.contact.get", {"id": int(contact_id)})
        cache[contact_id] = result if isinstance(result, dict) else None
    return cache[contact_id]


def get_company(bitrix: BitrixClient, company_id: str, cache: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    company_id = str(company_id or "").strip()
    if not company_id:
        return None
    if company_id not in cache:
        cache[company_id] = bitrix.get_company(company_id)
    return cache[company_id]


def get_company_bins(bitrix: BitrixClient, company: dict[str, Any] | None, cache: dict[str, list[str]]) -> list[str]:
    if not company:
        return []
    company_id = str(company.get("ID") or "")
    if company_id in cache:
        return cache[company_id]

    bins: list[str] = []
    for value in [company.get("ORIGIN_ID")]:
        bin_value = normalize_bin(value)
        if bin_value and bin_value not in bins:
            bins.append(bin_value)

    try:
        requisites = bitrix.list_requisites_for_company(company_id)
        for req in requisites:
            for key in ("RQ_BIN", "RQ_INN"):
                bin_value = normalize_bin(req.get(key))
                if bin_value and bin_value not in bins:
                    bins.append(bin_value)
    except Exception:
        pass

    cache[company_id] = bins
    return bins


def is_allowed(user_id: str, allowed_ids: set[str]) -> bool:
    return str(user_id or "").strip() in allowed_ids


def choose_target(
    group: DirectorGroup,
    *,
    manual_directors: dict[str, str],
    hard_bin_owners: dict[str, list[int]],
    allowed_ids: set[str],
    source_ids: set[str],
    target_policy: str,
) -> tuple[str, str]:
    # 1. Manual director fixation wins. This is the cleanest business rule.
    manual_owner = manual_directors.get(normalize_name(group.fio))
    if manual_owner and is_allowed(manual_owner, allowed_ids):
        return manual_owner, "manual_director_owner"

    # 2. Hard BIN if all known BINs point to exactly one same manager.
    hard_candidates: set[str] = set()
    hard_conflicting_bins: list[str] = []
    for company in group.companies.values():
        for bin_value in company.bins:
            owners = [str(x) for x in hard_bin_owners.get(bin_value, []) if str(x) in allowed_ids]
            if len(owners) == 1:
                hard_candidates.add(owners[0])
            elif len(owners) > 1:
                hard_conflicting_bins.append(bin_value)
    if len(hard_candidates) == 1 and not hard_conflicting_bins:
        return next(iter(hard_candidates)), "hard_bin_owner_unique"

    contact_owner = str(group.contact_owner_id or "")
    if target_policy in {"director_owner", "rules_then_director", "rules_then_majority"}:
        if contact_owner and contact_owner not in source_ids and is_allowed(contact_owner, allowed_ids):
            return contact_owner, "director_contact_owner"

    deal_owners = [deal.owner_id for deal in group.deals if deal.owner_id and deal.owner_id not in source_ids and is_allowed(deal.owner_id, allowed_ids)]
    deal_counter = Counter(deal_owners)
    if target_policy in {"unanimous_deal_owner", "rules_then_majority"}:
        if len(deal_counter) == 1:
            return next(iter(deal_counter.keys())), "unanimous_deal_owner"

    company_owners = [company.owner_id for company in group.companies.values() if company.owner_id and company.owner_id not in source_ids and is_allowed(company.owner_id, allowed_ids)]
    company_counter = Counter(company_owners)
    if len(company_counter) == 1 and target_policy in {"rules_then_majority", "unanimous_company_owner"}:
        return next(iter(company_counter.keys())), "unanimous_company_owner"

    if target_policy in {"majority", "rules_then_majority"}:
        weighted: Counter[str] = Counter()
        if contact_owner and contact_owner not in source_ids and is_allowed(contact_owner, allowed_ids):
            weighted[contact_owner] += 3
        for owner_id in deal_owners:
            weighted[owner_id] += 2
        for owner_id in company_owners:
            weighted[owner_id] += 1
        if weighted:
            top = weighted.most_common()
            if len(top) == 1 or top[0][1] > top[1][1]:
                return top[0][0], "weighted_majority_owner"

    return "", "no_safe_target_owner"


def build_groups(
    bitrix: BitrixClient,
    deals_raw: list[dict[str, Any]],
    *,
    user_names: dict[int, str],
) -> dict[str, DirectorGroup]:
    contact_cache: dict[str, dict[str, Any] | None] = {}
    company_cache: dict[str, dict[str, Any] | None] = {}
    company_bins_cache: dict[str, list[str]] = {}
    groups: dict[str, DirectorGroup] = {}

    for raw in deals_raw:
        deal_id = str(raw.get("ID") or "")
        contact_id = str(raw.get("CONTACT_ID") or "")
        if not contact_id:
            # Fallback for deals where Bitrix did not return primary CONTACT_ID.
            try:
                linked = bitrix.deal_contact_ids(deal_id)
                contact_id = sorted(linked)[0] if linked else ""
            except Exception:
                contact_id = ""
        if not contact_id:
            continue

        contact = get_contact(bitrix, contact_id, contact_cache)
        if not contact:
            continue

        fio = contact_fio(contact)
        contact_owner_id = str(contact.get("ASSIGNED_BY_ID") or "")
        if contact_id not in groups:
            groups[contact_id] = DirectorGroup(
                contact_id=contact_id,
                fio=fio,
                contact_owner_id=contact_owner_id,
                contact_owner_name=user_name(contact_owner_id, user_names),
            )
        group = groups[contact_id]

        deal = DealInfo(
            id=deal_id,
            title=str(raw.get("TITLE") or ""),
            company_id=str(raw.get("COMPANY_ID") or ""),
            contact_id=contact_id,
            owner_id=str(raw.get("ASSIGNED_BY_ID") or ""),
            owner_name=user_name(raw.get("ASSIGNED_BY_ID"), user_names),
            stage_id=str(raw.get("STAGE_ID") or ""),
            closed=str(raw.get("CLOSED") or ""),
            category_id=str(raw.get("CATEGORY_ID") or ""),
            origin_id=str(raw.get("ORIGIN_ID") or ""),
        )
        group.deals.append(deal)

        company_id = deal.company_id
        if company_id and company_id not in group.companies:
            company = get_company(bitrix, company_id, company_cache)
            if company:
                owner_id = str(company.get("ASSIGNED_BY_ID") or "")
                group.companies[company_id] = CompanyInfo(
                    id=company_id,
                    title=str(company.get("TITLE") or ""),
                    owner_id=owner_id,
                    owner_name=user_name(owner_id, user_names),
                    bins=get_company_bins(bitrix, company, company_bins_cache),
                )

    return groups


def group_is_consistent(group: DirectorGroup, target_owner_id: str | None = None) -> bool:
    expected = target_owner_id or group.contact_owner_id
    if not expected:
        return False
    if str(group.contact_owner_id or "") != str(expected):
        return False
    for company in group.companies.values():
        if str(company.owner_id or "") != str(expected):
            return False
    for deal in group.deals:
        if str(deal.owner_id or "") != str(expected):
            return False
    return True


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and repair owner consistency across director contact, companies and deals.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--only-eqazyna", action="store_true", default=False, help="Check only e-Qazyna deals")
    parser.add_argument("--deal-category-id", default="all", help="Deal category ID or all")
    parser.add_argument("--include-closed-deals", action="store_true", default=False)
    parser.add_argument("--target-policy", default="rules_then_majority", choices=["rules_then_majority", "director_owner", "unanimous_deal_owner", "unanimous_company_owner", "majority"])
    parser.add_argument("--repair", action="store_true", help="Update Bitrix records to the chosen target owner. Requires dry-run=false.")
    parser.add_argument("--source-responsible-ids", default="36,44", help="Technical/source user IDs, comma-separated")
    parser.add_argument("--max-deals", type=int, default=0)
    parser.add_argument("--out", default="exports/director_package_owner_actions.csv")
    parser.add_argument("--summary-out", default="exports/director_package_owner_summary.csv")
    parser.add_argument("--mismatch-out", default="exports/director_package_owner_mismatches.csv")
    args = parser.parse_args()

    webhook = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("BITRIX_WEBHOOK_URL is empty")

    manager_config = load_manager_config()
    allowed_ids = {str(x) for x in manager_config.allowed_user_ids}
    source_ids = parse_ids(args.source_responsible_ids) | {str(x) for x in manager_config.source_responsible_ids}
    user_names = manager_config.user_names
    manual_directors = load_manual_director_index()
    hard_bin_owners = load_hard_bin_owners()

    bitrix = BitrixClient(
        webhook_url=webhook,
        timeout=int(os.getenv("REQUEST_TIMEOUT", "60")),
        polite_delay_seconds=float(os.getenv("BITRIX_POLITE_DELAY_SECONDS", "0.2")),
    )

    deals_raw = list_deals(
        bitrix,
        only_eqazyna=args.only_eqazyna,
        deal_category_id=args.deal_category_id,
        include_closed_deals=args.include_closed_deals,
        max_deals=args.max_deals,
    )
    groups = build_groups(bitrix, deals_raw, user_names=user_names)

    summary_rows: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []

    for group in sorted(groups.values(), key=lambda g: int(g.contact_id)):
        target_id, target_reason = choose_target(
            group,
            manual_directors=manual_directors,
            hard_bin_owners=hard_bin_owners,
            allowed_ids=allowed_ids,
            source_ids=source_ids,
            target_policy=args.target_policy,
        )
        group.target_owner_id = target_id
        group.target_owner_name = user_name(target_id, user_names)
        group.target_reason = target_reason

        owners_present = {group.contact_owner_id}
        owners_present.update(company.owner_id for company in group.companies.values())
        owners_present.update(deal.owner_id for deal in group.deals)
        owners_present.discard("")

        consistent_to_current_director = group_is_consistent(group)
        consistent_to_target = group_is_consistent(group, target_id) if target_id else False
        has_mismatch = not consistent_to_current_director or (target_id and not consistent_to_target)
        group.status = "ok" if not has_mismatch else ("repairable" if target_id else "conflict_no_target")

        def add_mismatch(entity_type: str, entity_id: str, title: str, owner_id: str, owner_name_value: str, extra: dict[str, Any] | None = None) -> None:
            extra = extra or {}
            mismatch_rows.append(
                {
                    "director_contact_id": group.contact_id,
                    "director_fio": group.fio,
                    "director_owner_id": group.contact_owner_id,
                    "director_owner_name": group.contact_owner_name,
                    "target_owner_id": target_id,
                    "target_owner_name": group.target_owner_name,
                    "target_reason": target_reason,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "entity_title": title,
                    "entity_owner_id": owner_id,
                    "entity_owner_name": owner_name_value,
                    "stage_id": extra.get("stage_id", ""),
                    "closed": extra.get("closed", ""),
                    "company_id": extra.get("company_id", ""),
                    "company_title": extra.get("company_title", ""),
                    "origin_id": extra.get("origin_id", ""),
                    "url_hint": make_url_hint(entity_type, entity_id) if entity_id else "",
                }
            )

        if target_id and group.contact_owner_id != target_id:
            add_mismatch("contact", group.contact_id, group.fio, group.contact_owner_id, group.contact_owner_name)
        elif not consistent_to_current_director and not target_id:
            # Still show the director contact as the package root.
            add_mismatch("contact", group.contact_id, group.fio, group.contact_owner_id, group.contact_owner_name)

        for company in group.companies.values():
            if target_id and company.owner_id != target_id:
                add_mismatch("company", company.id, company.title, company.owner_id, company.owner_name)
            elif not target_id and company.owner_id != group.contact_owner_id:
                add_mismatch("company", company.id, company.title, company.owner_id, company.owner_name)

        for deal in group.deals:
            company = group.companies.get(deal.company_id)
            if target_id and deal.owner_id != target_id:
                add_mismatch(
                    "deal",
                    deal.id,
                    deal.title,
                    deal.owner_id,
                    deal.owner_name,
                    {
                        "stage_id": deal.stage_id,
                        "closed": deal.closed,
                        "company_id": deal.company_id,
                        "company_title": company.title if company else "",
                        "origin_id": deal.origin_id,
                    },
                )
            elif not target_id and deal.owner_id != group.contact_owner_id:
                add_mismatch(
                    "deal",
                    deal.id,
                    deal.title,
                    deal.owner_id,
                    deal.owner_name,
                    {
                        "stage_id": deal.stage_id,
                        "closed": deal.closed,
                        "company_id": deal.company_id,
                        "company_title": company.title if company else "",
                        "origin_id": deal.origin_id,
                    },
                )

        # Build actions. Only write if explicitly requested and safe target exists.
        if target_id:
            if group.contact_owner_id != target_id:
                action_rows.append({"entity_type": "contact", "entity_id": group.contact_id, "entity_title": group.fio, "old_owner_id": group.contact_owner_id, "old_owner_name": group.contact_owner_name, "new_owner_id": target_id, "new_owner_name": group.target_owner_name, "action": "dry_run_update" if args.dry_run or not args.repair else "updated", "reason": target_reason})
                if args.repair and not args.dry_run:
                    bitrix.update_contact(group.contact_id, {"ASSIGNED_BY_ID": int(target_id)})
            for company in group.companies.values():
                if company.owner_id != target_id:
                    action_rows.append({"entity_type": "company", "entity_id": company.id, "entity_title": company.title, "old_owner_id": company.owner_id, "old_owner_name": company.owner_name, "new_owner_id": target_id, "new_owner_name": group.target_owner_name, "action": "dry_run_update" if args.dry_run or not args.repair else "updated", "reason": target_reason})
                    if args.repair and not args.dry_run:
                        bitrix.update_company(company.id, {"ASSIGNED_BY_ID": int(target_id)})
            for deal in group.deals:
                if deal.owner_id != target_id:
                    action_rows.append({"entity_type": "deal", "entity_id": deal.id, "entity_title": deal.title, "old_owner_id": deal.owner_id, "old_owner_name": deal.owner_name, "new_owner_id": target_id, "new_owner_name": group.target_owner_name, "action": "dry_run_update" if args.dry_run or not args.repair else "updated", "reason": target_reason})
                    if args.repair and not args.dry_run:
                        bitrix.update_deal(deal.id, {"ASSIGNED_BY_ID": int(target_id)})
        elif has_mismatch:
            action_rows.append({"entity_type": "director_package", "entity_id": group.contact_id, "entity_title": group.fio, "old_owner_id": group.contact_owner_id, "old_owner_name": group.contact_owner_name, "new_owner_id": "", "new_owner_name": "", "action": "skipped_no_safe_target", "reason": target_reason})

        group.mismatch_count = sum(1 for row in mismatch_rows if row["director_contact_id"] == group.contact_id)
        group.action_count = sum(1 for row in action_rows if row.get("entity_id") == group.contact_id or row.get("reason") == target_reason)

        summary_rows.append(
            {
                "director_contact_id": group.contact_id,
                "director_fio": group.fio,
                "director_owner_id": group.contact_owner_id,
                "director_owner_name": group.contact_owner_name,
                "target_owner_id": target_id,
                "target_owner_name": group.target_owner_name,
                "target_reason": target_reason,
                "status": group.status,
                "owners_present_ids": ",".join(sorted(owners_present, key=lambda x: int(x) if x.isdigit() else 999999)),
                "owners_present_names": ", ".join(user_name(x, user_names) for x in sorted(owners_present, key=lambda x: int(x) if x.isdigit() else 999999)),
                "companies_count": len(group.companies),
                "deals_count": len(group.deals),
                "mismatch_count": group.mismatch_count,
                "company_ids": ",".join(sorted(group.companies.keys(), key=lambda x: int(x) if x.isdigit() else 999999)),
                "deal_ids": ",".join(deal.id for deal in group.deals),
            }
        )

    write_csv(
        Path(args.summary_out),
        summary_rows,
        [
            "director_contact_id",
            "director_fio",
            "director_owner_id",
            "director_owner_name",
            "target_owner_id",
            "target_owner_name",
            "target_reason",
            "status",
            "owners_present_ids",
            "owners_present_names",
            "companies_count",
            "deals_count",
            "mismatch_count",
            "company_ids",
            "deal_ids",
        ],
    )
    write_csv(
        Path(args.mismatch_out),
        mismatch_rows,
        [
            "director_contact_id",
            "director_fio",
            "director_owner_id",
            "director_owner_name",
            "target_owner_id",
            "target_owner_name",
            "target_reason",
            "entity_type",
            "entity_id",
            "entity_title",
            "entity_owner_id",
            "entity_owner_name",
            "stage_id",
            "closed",
            "company_id",
            "company_title",
            "origin_id",
            "url_hint",
        ],
    )
    write_csv(
        Path(args.out),
        action_rows,
        [
            "entity_type",
            "entity_id",
            "entity_title",
            "old_owner_id",
            "old_owner_name",
            "new_owner_id",
            "new_owner_name",
            "action",
            "reason",
        ],
    )

    print(f"DIRECTOR_PACKAGES={len(groups)}")
    print(f"SUMMARY_ROWS={len(summary_rows)}")
    print(f"MISMATCH_ROWS={len(mismatch_rows)}")
    print(f"ACTION_ROWS={len(action_rows)}")
    if args.repair and not args.dry_run:
        print("WRITE_MODE=enabled")
    else:
        print("WRITE_MODE=dry_run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
