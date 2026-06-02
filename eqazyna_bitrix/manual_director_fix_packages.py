from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit_repair_deal_packages import (
    build_company_index,
    collect_group_contacts,
    get_contact,
    is_closed_deal,
    list_all_eqazyna_deals,
    make_records,
    owner_id,
    record_bin,
    record_director,
    safe_str,
    update_company_owner,
    update_contact_owner,
    update_deal_owner,
    user_name,
)
from .bitrix_client import BitrixClient
from .distribute_companies import ALLOWED_USER_IDS, HARD_BIN_OWNERS, USER_NAMES, _normalize_text
from .config.assignment import load_manual_director_owners_raw
from .director import director_identity_key
from .settings import Settings


# ---------------------------------------------------------------------
# РУЧНЫЕ ЖЁСТКИЕ ФИКСАЦИИ ПО РУКОВОДИТЕЛЮ
# ---------------------------------------------------------------------
# Список хранится в eqazyna_bitrix/config/manual_directors.yml.
# Этот файл содержит только исполнительную логику и совместимый словарь.

MANUAL_DIRECTOR_OWNERS_RAW = load_manual_director_owners_raw()

def build_manual_director_owners() -> dict[str, int]:
    index: dict[str, int] = {}
    conflicts: dict[str, list[int]] = defaultdict(list)

    for user_id, names in MANUAL_DIRECTOR_OWNERS_RAW.items():
        if user_id not in ALLOWED_USER_IDS:
            raise SystemExit(f"Manual target user {user_id} is not in ALLOWED_USER_IDS")
        for name in names:
            normalized = director_identity_key(name) or _normalize_text(name)
            if not normalized:
                continue
            if normalized in index and index[normalized] != user_id:
                conflicts[normalized].extend([index[normalized], user_id])
            index[normalized] = user_id

    if conflicts:
        raise SystemExit(f"Duplicate manual director mapping: {dict(conflicts)}")

    return index


MANUAL_DIRECTOR_OWNERS = build_manual_director_owners()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply manual hard director-owner fixes to e-Qazyna company/deal packages in Bitrix"
    )
    parser.add_argument("--dry-run", action="store_true", help="Only calculate changes, do not write to Bitrix")
    parser.add_argument("--out", default="exports/manual_director_fix_packages_log.json")
    parser.add_argument("--csv-out", default="exports/manual_director_fix_packages_groups.csv")
    parser.add_argument(
        "--include-closed-deals",
        action="store_true",
        help="Also rewrite closed deals. Default: skip closed deals.",
    )
    parser.add_argument(
        "--sync-contacts",
        action="store_true",
        help="Also rewrite linked contacts to the manual target owner. Slower.",
    )
    return parser.parse_args()


def action_counter(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for action in row.get("actions", []):
            counter[safe_str(action.get("action"))] += 1
    return dict(counter)


def actions_to_change(row: dict[str, Any]) -> int:
    return sum(
        1
        for action in row.get("actions", [])
        if safe_str(action.get("action")).startswith(("dry_run_update", "updated_"))
    )


def hard_bin_debug_for_bins(bins: set[str], target_user_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hard_bins: dict[str, Any] = {}
    overridden: list[dict[str, Any]] = []

    for bin_value in sorted(bins):
        owner_ids = sorted(set(HARD_BIN_OWNERS.get(bin_value, [])))
        if not owner_ids:
            continue
        owner_names = [user_name(user_id) for user_id in owner_ids]
        hard_bins[bin_value] = {
            "owner_ids": owner_ids,
            "owner_names": owner_names,
        }
        if target_user_id not in owner_ids:
            overridden.append(
                {
                    "bin": bin_value,
                    "hard_owner_ids": owner_ids,
                    "hard_owner_names": owner_names,
                    "manual_target_user_id": target_user_id,
                    "manual_target_user_name": user_name(target_user_id),
                }
            )

    return hard_bins, overridden


def apply_manual_group(
    *,
    client: BitrixClient,
    director_key: str,
    director_names: list[str],
    records: list[dict[str, Any]],
    target_user_id: int,
    dry_run: bool,
    include_closed_deals: bool,
    sync_contacts: bool,
) -> dict[str, Any]:
    company_records = {
        safe_str(record.get("company_id")): record
        for record in records
        if record.get("entity_type") == "company" and safe_str(record.get("company_id")) not in {"", "0"}
    }
    deal_records = [record for record in records if record.get("entity_type") == "deal"]

    owners_before = sorted({record.get("owner_id") for record in records if isinstance(record.get("owner_id"), int)})
    bins = sorted({record_bin(record) for record in records if record_bin(record)})
    hard_bins, overridden_hard_bins = hard_bin_debug_for_bins(set(bins), target_user_id)

    row: dict[str, Any] = {
        "group_key": f"manual_director|{director_key}",
        "group_type": "manual_director",
        "director_key": director_key,
        "director_names": sorted(set(director_names)),
        "bins": bins,
        "target_user_id": target_user_id,
        "target_user_name": user_name(target_user_id),
        "reason": "manual_director_owner",
        "reason_debug": {
            "manual_director_owner": target_user_id,
            "manual_director_owner_name": user_name(target_user_id),
            "configured_aliases": [
                alias
                for alias, user_id in sorted(MANUAL_DIRECTOR_OWNERS.items())
                if user_id == target_user_id
            ],
            "hard_bins_in_package": hard_bins,
            "overridden_hard_bins": overridden_hard_bins,
        },
        "owners_before": owners_before,
        "owners_before_names": [user_name(owner) for owner in owners_before],
        "is_split_before": len(set(owners_before)) > 1,
        "company_count": len(company_records),
        "deal_count": len(deal_records),
        "actions": [],
        "errors": [],
    }

    for company_id, record in sorted(company_records.items(), key=lambda item: int(item[0])):
        old_owner = record.get("owner_id")
        action = {
            "entity_type": "company",
            "entity_id": company_id,
            "title": record.get("title"),
            "company_id": company_id,
            "closed": False,
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
        for contact_id in sorted(collect_group_contacts(client, records), key=lambda x: int(x)):
            old_owner: int | None = None
            title = ""
            try:
                contact = get_contact(client, contact_id)
                if contact:
                    old_owner = owner_id(contact)
                    title = " ".join(
                        part
                        for part in [
                            safe_str(contact.get("LAST_NAME")),
                            safe_str(contact.get("NAME")),
                            safe_str(contact.get("SECOND_NAME")),
                        ]
                        if part
                    ).strip()
            except Exception as exc:  # noqa: BLE001
                row["errors"].append(
                    {"entity_type": "contact", "entity_id": contact_id, "action": "get_contact_error", "error": str(exc)}
                )
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


def write_csv(rows: list[dict[str, Any]], csv_out: str) -> None:
    path = Path(csv_out)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "director_names",
                "target_user_name",
                "bins",
                "owners_before_names",
                "is_split_before",
                "company_count",
                "deal_count",
                "actions_to_change",
                "overridden_hard_bins",
                "errors_count",
            ],
        )
        writer.writeheader()
        for row in rows:
            overridden = row.get("reason_debug", {}).get("overridden_hard_bins", [])
            writer.writerow(
                {
                    "director_names": ", ".join(row.get("director_names", [])),
                    "target_user_name": row.get("target_user_name"),
                    "bins": ", ".join(row.get("bins", [])),
                    "owners_before_names": ", ".join(row.get("owners_before_names", [])),
                    "is_split_before": row.get("is_split_before"),
                    "company_count": row.get("company_count"),
                    "deal_count": row.get("deal_count"),
                    "actions_to_change": actions_to_change(row),
                    "overridden_hard_bins": json.dumps(overridden, ensure_ascii=False),
                    "errors_count": len(row.get("errors", [])),
                }
            )


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")

    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)

    print("Fetching e-Qazyna deals...")
    deals = list_all_eqazyna_deals(client)
    print(f"Deals found: {len(deals)}")

    print("Fetching companies and requisites...")
    companies = build_company_index(client, deals)
    print(f"Companies found/referenced: {len(companies)}")

    records, _companies, _company_deals = make_records(deals, companies)

    manual_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    director_names: dict[str, set[str]] = defaultdict(set)

    for record in records:
        director = record_director(record)
        if not director:
            continue
        key = director_identity_key(director) or _normalize_text(director)
        target_user_id = MANUAL_DIRECTOR_OWNERS.get(key)
        if target_user_id is None:
            continue
        manual_groups[key].append(record)
        director_names[key].add(director)

    rows: list[dict[str, Any]] = []
    for director_key, group_records in sorted(manual_groups.items()):
        target_user_id = MANUAL_DIRECTOR_OWNERS[director_key]
        row = apply_manual_group(
            client=client,
            director_key=director_key,
            director_names=sorted(director_names[director_key]),
            records=group_records,
            target_user_id=target_user_id,
            dry_run=args.dry_run,
            include_closed_deals=args.include_closed_deals,
            sync_contacts=args.sync_contacts,
        )
        rows.append(row)

    found_manual_keys = set(manual_groups)
    not_found = [
        {
            "alias": alias,
            "target_user_id": user_id,
            "target_user_name": user_name(user_id),
        }
        for alias, user_id in sorted(MANUAL_DIRECTOR_OWNERS.items())
        if alias not in found_manual_keys
    ]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "include_closed_deals": args.include_closed_deals,
        "sync_contacts": args.sync_contacts,
        "manual_director_count_configured": len(MANUAL_DIRECTOR_OWNERS),
        "manual_director_groups_found": len(rows),
        "manual_director_aliases_not_found": not_found,
        "allowed_user_ids": ALLOWED_USER_IDS,
        "allowed_users": {str(user_id): user_name(user_id) for user_id in ALLOWED_USER_IDS},
        "manual_director_owners": {
            alias: {
                "target_user_id": user_id,
                "target_user_name": user_name(user_id),
            }
            for alias, user_id in sorted(MANUAL_DIRECTOR_OWNERS.items())
        },
        "action_counts": action_counter(rows),
        "total_actions_to_change": sum(actions_to_change(row) for row in rows),
        "total_errors": sum(len(row.get("errors", [])) for row in rows),
        "distribution_mode": "manual_hard_director_owner_fix_no_limits_manual_director_overrides_duplicate_hard_bin_conflict",
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
