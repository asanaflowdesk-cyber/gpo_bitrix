#!/usr/bin/env python3
"""Reassign multiple founder CRM packages to automatically selected managers.

Input anchor: one or more exact Bitrix contact IDs of founders/directors. IDs may
be separated by commas, semicolons, spaces or new lines.

For every founder, the script:
- discovers all directly linked companies;
- collects those companies, all their deals and linked contacts;
- selects one eligible manager from active managers in managers.yml;
- assigns the founder's entire package to that manager;
- adds a service comment to the founder contact timeline.

Manager selection is deterministic-pseudorandom for the same founder ID and
candidate pool. Therefore dry-run and write runs select the same manager.
All packages are discovered before any write. If packages overlap and would be
assigned to different managers, the script stops before changing Bitrix.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple
from urllib.parse import urlencode

from .manager_config import load_manager_config
from .reassign_by_company_id_batch import (
    Bitrix,
    BitrixError,
    chunked,
    make_row,
    norm_id,
    parse_bool,
    parse_id_set,
    text,
)

MOVED_STATUSES = {"owner_changed_and_verified", "update_sent_not_verified"}


def sorted_numeric(values: Iterable[str]) -> List[str]:
    return sorted({norm_id(value) for value in values if norm_id(value)}, key=int)


def parse_founder_contact_ids(raw: Any) -> List[str]:
    """Parse numeric IDs while preserving the user's order and removing duplicates."""
    values = re.split(r"[\s,;]+", text(raw))
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        normalized = norm_id(value)
        if not normalized:
            continue
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ValueError(f"Invalid founder contact ID: {value!r}")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if not result:
        raise ValueError("At least one founder contact ID is required")
    return result


def get_founder_company_ids(bx: Bitrix, founder_contact: Dict[str, Any]) -> Set[str]:
    """Read all direct and secondary company links of a founder contact."""
    contact_id = norm_id(founder_contact.get("ID"))
    company_ids: Set[str] = set()

    primary_company_id = norm_id(founder_contact.get("COMPANY_ID"))
    if primary_company_id:
        company_ids.add(primary_company_id)

    result = bx.call_form("crm.contact.company.items.get", [("id", contact_id)])
    if not isinstance(result, list):
        raise BitrixError(
            f"crm.contact.company.items.get returned unexpected result for contact #{contact_id}: {result!r}"
        )
    for item in result:
        if not isinstance(item, dict):
            continue
        company_id = norm_id(item.get("COMPANY_ID") or item.get("ID"))
        if company_id:
            company_ids.add(company_id)

    return company_ids


def candidate_manager_ids(
    active_manager_ids: Sequence[int | str],
    explicit_excluded_ids: Set[str],
    current_founder_owner_id: str,
) -> List[str]:
    """Build an eligible target pool and exclude the current founder owner."""
    excluded = set(explicit_excluded_ids)
    if current_founder_owner_id:
        excluded.add(current_founder_owner_id)
    return [str(user_id) for user_id in active_manager_ids if str(user_id) not in excluded]


def stable_candidate_order(founder_contact_id: str, candidate_ids: Sequence[str]) -> List[str]:
    """Return a stable pseudorandom rotation of the sorted candidate pool."""
    clean = sorted_numeric(candidate_ids)
    if not clean:
        return []
    material = f"founder-package-v2|{founder_contact_id}|{','.join(clean)}".encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % len(clean)
    return clean[offset:] + clean[:offset]


def validate_manager_pool(
    bx: Bitrix,
    active_manager_ids: Sequence[int],
    explicit_excluded_ids: Set[str],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Validate each configured candidate only once for the whole batch."""
    labels: Dict[str, str] = {}
    rejected: Dict[str, str] = {}
    for user_id in sorted_numeric(str(value) for value in active_manager_ids):
        if user_id in explicit_excluded_ids:
            continue
        try:
            labels[user_id] = bx.validate_user(user_id)
        except Exception as exc:  # noqa: BLE001
            rejected[user_id] = str(exc)[:500]
    if not labels:
        raise ValueError(
            "No eligible active managers remain after exclusions. Validation errors: "
            + json.dumps(rejected, ensure_ascii=False)
        )
    return labels, rejected


def select_target_manager_from_pool(
    founder_contact_id: str,
    validated_manager_labels: Dict[str, str],
    current_founder_owner_id: str,
) -> Tuple[str, str, List[str]]:
    candidates = candidate_manager_ids(
        active_manager_ids=list(validated_manager_labels),
        explicit_excluded_ids=set(),
        current_founder_owner_id=current_founder_owner_id,
    )
    ordered = stable_candidate_order(founder_contact_id, candidates)
    if not ordered:
        raise ValueError(
            f"No eligible manager remains for founder #{founder_contact_id}; "
            f"current owner #{current_founder_owner_id or 'NONE'} is the only available candidate"
        )
    target_user_id = ordered[0]
    return target_user_id, validated_manager_labels[target_user_id], ordered


def collect_package(
    bx: Bitrix,
    founder_contact: Dict[str, Any],
    company_ids: Set[str],
    include_closed_deals: bool,
    include_deal_contacts: bool,
    deal_originator_id: str,
    max_list_pages: int,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Collect all companies, their deals and linked contacts."""
    companies = bx.batch_get_entities("company", sorted_numeric(company_ids))
    missing_company_ids = set(company_ids) - set(companies)
    if missing_company_ids:
        raise BitrixError(
            "Failed to read linked companies: " + ", ".join(sorted_numeric(missing_company_ids))
        )

    deals: Dict[str, Dict[str, Any]] = {}
    contact_ids: Set[str] = {norm_id(founder_contact.get("ID"))}

    for company_id in sorted_numeric(companies):
        company_deals = bx.list_company_deals(
            company_id=company_id,
            include_closed_deals=include_closed_deals,
            deal_originator_id=deal_originator_id,
            max_pages=max_list_pages,
        )
        deals.update(company_deals)
        contact_ids.update(bx.list_direct_company_contact_ids(company_id, max_pages=max_list_pages))

    for deal in deals.values():
        primary_contact_id = norm_id(deal.get("CONTACT_ID"))
        if primary_contact_id:
            contact_ids.add(primary_contact_id)

    if include_deal_contacts and deals:
        contact_ids.update(bx.list_deal_contact_ids(sorted_numeric(deals)))

    contacts = bx.batch_get_entities("contact", sorted_numeric(contact_ids))
    missing_contact_ids = contact_ids - set(contacts)
    if missing_contact_ids:
        raise BitrixError(
            "Failed to read linked contacts: " + ", ".join(sorted_numeric(missing_contact_ids))
        )

    return companies, deals, contacts


def build_rows(
    founder_contact_id: str,
    companies: Dict[str, Dict[str, Any]],
    deals: Dict[str, Dict[str, Any]],
    contacts: Dict[str, Dict[str, Any]],
    target_user_id: str,
    target_user_name: str,
    dry_run: bool,
    excluded_company_ids: Set[str],
    excluded_deal_ids: Set[str],
    excluded_contact_ids: Set[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def append_row(
        entity_type: str,
        entity: Dict[str, Any],
        relation: str,
        excluded: bool,
    ) -> None:
        row = make_row(
            entity_type,
            entity,
            relation,
            target_user_id,
            target_user_name,
            dry_run,
            excluded,
        )
        row["founder_contact_id"] = founder_contact_id
        rows.append(row)

    for company_id in sorted_numeric(companies):
        append_row(
            "company",
            companies[company_id],
            "company_linked_to_founder_contact",
            company_id in excluded_company_ids,
        )

    for deal_id in sorted_numeric(deals):
        append_row(
            "deal",
            deals[deal_id],
            "deal_linked_to_founder_company",
            deal_id in excluded_deal_ids,
        )

    for contact_id in sorted_numeric(contacts):
        append_row(
            "contact",
            contacts[contact_id],
            "contact_linked_to_founder_or_company_package",
            contact_id in excluded_contact_ids,
        )

    return rows


def find_cross_package_conflicts(packages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find shared non-excluded entities that would get different owners."""
    seen: Dict[Tuple[str, str], Dict[str, str]] = {}
    conflicts: List[Dict[str, Any]] = []
    for package in packages:
        founder_contact_id = text(package.get("founder_contact_id"))
        target_user_id = text(package.get("target_user_id"))
        for row in package.get("rows", []):
            if text(row.get("action_status")) == "excluded":
                continue
            key = (text(row.get("entity_type")), norm_id(row.get("entity_id")))
            previous = seen.get(key)
            current = {
                "founder_contact_id": founder_contact_id,
                "target_user_id": target_user_id,
            }
            if previous and previous["target_user_id"] != target_user_id:
                conflicts.append(
                    {
                        "entity_type": key[0],
                        "entity_id": key[1],
                        "first_founder_contact_id": previous["founder_contact_id"],
                        "first_target_user_id": previous["target_user_id"],
                        "second_founder_contact_id": founder_contact_id,
                        "second_target_user_id": target_user_id,
                    }
                )
            else:
                seen[key] = current
    return conflicts


def build_timeline_comment(
    rows: List[Dict[str, Any]],
    founder_contact_id: str,
    founder_owner_before: str,
    target_user_id: str,
    target_user_name: str,
    user_labels: Dict[str, str],
    explicit_excluded_manager_ids: Set[str],
    company_ids: Set[str],
    batch_size: int,
) -> str:
    moved_rows = [row for row in rows if text(row.get("action_status")) in MOVED_STATUSES]
    moved_counts = {"company": 0, "deal": 0, "contact": 0}
    excluded_counts = {"company": 0, "deal": 0, "contact": 0}

    for row in rows:
        entity_type = text(row.get("entity_type"))
        if entity_type not in moved_counts:
            continue
        status = text(row.get("action_status"))
        if status in MOVED_STATUSES:
            moved_counts[entity_type] += 1
        elif status == "excluded":
            excluded_counts[entity_type] += 1

    previous_owner_ids = sorted_numeric(
        row.get("before_owner_id") for row in moved_rows if norm_id(row.get("before_owner_id"))
    )

    def label(user_id: str) -> str:
        if not user_id:
            return "не указан"
        return f"{user_labels.get(user_id, f'ID {user_id}')} (ID {user_id})"

    previous_owners = ", ".join(label(user_id) for user_id in previous_owner_ids) or "не указаны"
    excluded_manager_text = (
        ", ".join(label(user_id) for user_id in sorted_numeric(explicit_excluded_manager_ids))
        if explicit_excluded_manager_ids
        else "нет"
    )

    lines = [
        "Служебная отметка об автоматическом перераспределении.",
        "",
        f"Пакет учредителя переназначен в пакетном запуске ({batch_size} учредителей).",
        f"Ответственный карточки учредителя до запуска: {label(founder_owner_before)}.",
        f"Предыдущие ответственные связанных элементов: {previous_owners}.",
        f"Новый ответственный: {target_user_name} (ID {target_user_id}).",
        f"Исключены из выбора менеджеры: {excluded_manager_text}.",
        "",
        f"Связанные компании: {', '.join('#' + value for value in sorted_numeric(company_ids))}.",
        (
            f"Перенесено: компаний — {moved_counts['company']}, "
            f"сделок — {moved_counts['deal']}, контактов — {moved_counts['contact']}."
        ),
    ]
    if any(excluded_counts.values()):
        lines.append(
            f"Исключено из переноса: компаний — {excluded_counts['company']}, "
            f"сделок — {excluded_counts['deal']}, контактов — {excluded_counts['contact']}."
        )
    lines.append(
        f"Основание: пакетное перераспределение по карточке учредителя #{founder_contact_id}."
    )
    return "\n".join(lines)


def add_timeline_comments_batch(
    bx: Bitrix,
    comments: Sequence[Tuple[str, str]],
) -> Dict[str, Dict[str, str]]:
    """Add founder timeline comments in REST batches of up to 50 commands."""
    results: Dict[str, Dict[str, str]] = {}
    for part in chunked(list(comments), 50):
        commands: Dict[str, str] = {}
        key_to_contact: Dict[str, str] = {}
        for index, (contact_id, comment) in enumerate(part, start=1):
            key = f"c{index}"
            commands[key] = "crm.timeline.comment.add?" + urlencode(
                {
                    "fields[ENTITY_ID]": contact_id,
                    "fields[ENTITY_TYPE]": "contact",
                    "fields[COMMENT]": comment,
                }
            )
            key_to_contact[key] = contact_id
        payload = bx.batch(commands)
        batch_result = payload.get("result") or {}
        batch_errors = payload.get("result_error") or {}
        for key, contact_id in key_to_contact.items():
            if key in batch_errors:
                results[contact_id] = {
                    "status": "failed",
                    "comment_id": "",
                    "error": json.dumps(batch_errors[key], ensure_ascii=False)[:2000],
                }
            else:
                comment_id = text(batch_result.get(key))
                results[contact_id] = {
                    "status": "added" if comment_id else "failed",
                    "comment_id": comment_id,
                    "error": "" if comment_id else "crm.timeline.comment.add returned empty result",
                }
    return results


def write_batch_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "founder_contact_id",
        "entity_type",
        "entity_id",
        "entity_title",
        "relation",
        "target_user_id",
        "target_user_name",
        "dry_run",
        "before_owner_id",
        "update_result",
        "verified_owner_id",
        "final_owner_id",
        "action_status",
        "error",
        "company_id",
        "contact_id_field",
        "originator_id",
        "origin_id",
        "category_id",
        "stage_id",
        "closed",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def package_summary(package: Dict[str, Any]) -> Dict[str, Any]:
    rows = list(package.get("rows", []))
    by_type: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    errors: List[Dict[str, str]] = []
    for row in rows:
        entity_type = text(row.get("entity_type"))
        status = text(row.get("action_status"))
        by_type[entity_type] = by_type.get(entity_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        if text(row.get("error")):
            errors.append(
                {
                    "entity_type": entity_type,
                    "entity_id": text(row.get("entity_id")),
                    "error": text(row.get("error")),
                }
            )
    return {
        "founder_contact_id": package["founder_contact_id"],
        "founder_owner_before": package["founder_owner_before"],
        "company_ids": sorted_numeric(package["company_ids"]),
        "target_user_id": package["target_user_id"],
        "target_user_name": package["target_user_name"],
        "candidate_order": list(package["candidate_order"]),
        "total_rows": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "timeline_comment": package["timeline_comment"],
        "errors": errors[:100],
    }


def make_summary(
    packages: Sequence[Dict[str, Any]],
    founder_contact_ids: Sequence[str],
    dry_run: bool,
    active_manager_ids: Sequence[int],
    explicit_excluded_manager_ids: Set[str],
    rejected_candidates: Dict[str, str],
    conflicts: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    all_rows = [row for package in packages for row in package.get("rows", [])]
    by_status: Dict[str, int] = {}
    for row in all_rows:
        status = text(row.get("action_status"))
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "founder_contact_ids": list(founder_contact_ids),
        "founder_count": len(founder_contact_ids),
        "selection_mode": "stable_pseudorandom_per_founder_contact_id",
        "dry_run": dry_run,
        "active_manager_ids_from_config": [str(value) for value in active_manager_ids],
        "explicit_excluded_manager_ids": sorted_numeric(explicit_excluded_manager_ids),
        "rejected_manager_candidates": rejected_candidates,
        "total_rows": len(all_rows),
        "by_status": by_status,
        "cross_package_conflicts": list(conflicts),
        "packages": [package_summary(package) for package in packages],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatically reassign multiple founder packages by Bitrix contact IDs"
    )
    parser.add_argument("--founder-contact-ids", default="")
    parser.add_argument(
        "--founder-contact-id",
        default="",
        help="Backward-compatible single ID; prefer --founder-contact-ids",
    )
    parser.add_argument("--exclude-manager-ids", default="")
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--exclude-company-ids", default="")
    parser.add_argument("--exclude-deal-ids", default="")
    parser.add_argument("--exclude-contact-ids", default="")
    parser.add_argument("--deal-originator-id", default="", help="Empty = all company deals")
    parser.add_argument("--include-closed-deals", default="true")
    parser.add_argument("--include-deal-contacts", default="true")
    parser.add_argument("--verify", default="true")
    parser.add_argument("--add-timeline-comment", default="true")
    parser.add_argument("--max-list-pages", default="100")
    parser.add_argument("--managers-config", default="")
    parser.add_argument("--out", default="exports/reassign_founder_packages_random_log.csv")
    parser.add_argument("--json-out", default="exports/reassign_founder_packages_random_summary.json")
    args = parser.parse_args()

    raw_founder_ids = text(args.founder_contact_ids) or text(args.founder_contact_id)
    founder_contact_ids = parse_founder_contact_ids(raw_founder_ids)
    dry_run = parse_bool(args.dry_run, default=True)
    include_closed_deals = parse_bool(args.include_closed_deals, default=True)
    include_deal_contacts = parse_bool(args.include_deal_contacts, default=True)
    verify = parse_bool(args.verify, default=True)
    add_timeline_comment = parse_bool(args.add_timeline_comment, default=True)
    deal_originator_id = text(args.deal_originator_id)
    max_list_pages = int(args.max_list_pages)

    explicit_excluded_manager_ids = parse_id_set(args.exclude_manager_ids)
    excluded_company_ids = parse_id_set(args.exclude_company_ids)
    excluded_deal_ids = parse_id_set(args.exclude_deal_ids)
    excluded_contact_ids = parse_id_set(args.exclude_contact_ids)

    if max_list_pages <= 0:
        raise ValueError("max_list_pages must be positive")

    config = load_manager_config(args.managers_config or None)
    bx = Bitrix(os.getenv("BITRIX_WEBHOOK_URL", ""), timeout=int(os.getenv("REQUEST_TIMEOUT", "60")))
    validated_manager_labels, rejected_candidates = validate_manager_pool(
        bx=bx,
        active_manager_ids=config.allowed_user_ids,
        explicit_excluded_ids=explicit_excluded_manager_ids,
    )

    packages: List[Dict[str, Any]] = []
    print(f"MODE: {'DRY_RUN' if dry_run else 'WRITE'}")
    print(f"FOUNDER_CONTACT_IDS: {founder_contact_ids}")
    print(f"VALIDATED_MANAGER_IDS: {sorted_numeric(validated_manager_labels)}")
    print(f"EXCLUDED_MANAGER_IDS: {sorted_numeric(explicit_excluded_manager_ids)}")

    # Preflight: discover every package before changing any CRM record.
    for index, founder_contact_id in enumerate(founder_contact_ids, start=1):
        print(f"[{index}/{len(founder_contact_ids)}] Discover founder #{founder_contact_id}")
        founder_contact = bx.get_contact(founder_contact_id)
        founder_owner_before = norm_id(founder_contact.get("ASSIGNED_BY_ID"))
        company_ids = get_founder_company_ids(bx, founder_contact)
        if not company_ids:
            raise ValueError(f"No companies are linked to founder contact #{founder_contact_id}")

        target_user_id, target_user_name, candidate_order = select_target_manager_from_pool(
            founder_contact_id=founder_contact_id,
            validated_manager_labels=validated_manager_labels,
            current_founder_owner_id=founder_owner_before,
        )

        companies, deals, contacts = collect_package(
            bx=bx,
            founder_contact=founder_contact,
            company_ids=company_ids,
            include_closed_deals=include_closed_deals,
            include_deal_contacts=include_deal_contacts,
            deal_originator_id=deal_originator_id,
            max_list_pages=max_list_pages,
        )
        rows = build_rows(
            founder_contact_id=founder_contact_id,
            companies=companies,
            deals=deals,
            contacts=contacts,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            dry_run=dry_run,
            excluded_company_ids=excluded_company_ids,
            excluded_deal_ids=excluded_deal_ids,
            excluded_contact_ids=excluded_contact_ids,
        )
        packages.append(
            {
                "founder_contact_id": founder_contact_id,
                "founder_owner_before": founder_owner_before,
                "company_ids": company_ids,
                "target_user_id": target_user_id,
                "target_user_name": target_user_name,
                "candidate_order": candidate_order,
                "rows": rows,
                "timeline_comment": {
                    "contact_id": founder_contact_id,
                    "enabled": add_timeline_comment,
                    "status": "dry_run_not_added" if dry_run else "not_attempted",
                    "comment_id": "",
                    "error": "",
                },
            }
        )
        print(
            f"[{index}/{len(founder_contact_ids)}] founder #{founder_contact_id}: "
            f"companies={len(companies)}, deals={len(deals)}, contacts={len(contacts)}, "
            f"target={target_user_id} ({target_user_name})"
        )

    conflicts = find_cross_package_conflicts(packages)
    if conflicts:
        raise ValueError(
            "Founder packages overlap and selected different target managers. "
            "No Bitrix records were changed. Conflicts: "
            + json.dumps(conflicts[:50], ensure_ascii=False)
        )

    all_rows = [row for package in packages for row in package["rows"]]
    if dry_run:
        for row in all_rows:
            if text(row.get("action_status")) != "excluded":
                row["action_status"] = "dry_run_planned"
                row["final_owner_id"] = row.get("before_owner_id", "")
    else:
        # Group by selected manager so each target is updated in efficient REST batches.
        for target_user_id in sorted_numeric(package["target_user_id"] for package in packages):
            target_rows = [
                row for row in all_rows if norm_id(row.get("target_user_id")) == target_user_id
            ]
            print(f"WRITE target #{target_user_id}: {len(target_rows)} CRM records")
            bx.batch_update_owners(target_rows, target_user_id, verify=verify)

        if not add_timeline_comment:
            for package in packages:
                package["timeline_comment"]["status"] = "disabled"
        else:
            user_ids_for_labels: Set[str] = set(explicit_excluded_manager_ids)
            for package in packages:
                user_ids_for_labels.add(package["target_user_id"])
                if package["founder_owner_before"]:
                    user_ids_for_labels.add(package["founder_owner_before"])
                user_ids_for_labels.update(
                    norm_id(row.get("before_owner_id"))
                    for row in package["rows"]
                    if norm_id(row.get("before_owner_id"))
                )
            user_labels = bx.get_user_labels(user_ids_for_labels)

            pending_comments: List[Tuple[str, str]] = []
            for package in packages:
                moved_rows = [
                    row for row in package["rows"] if text(row.get("action_status")) in MOVED_STATUSES
                ]
                if not moved_rows:
                    package["timeline_comment"]["status"] = "skipped_no_owner_changes"
                    continue
                pending_comments.append(
                    (
                        package["founder_contact_id"],
                        build_timeline_comment(
                            rows=package["rows"],
                            founder_contact_id=package["founder_contact_id"],
                            founder_owner_before=package["founder_owner_before"],
                            target_user_id=package["target_user_id"],
                            target_user_name=package["target_user_name"],
                            user_labels=user_labels,
                            explicit_excluded_manager_ids=explicit_excluded_manager_ids,
                            company_ids=package["company_ids"],
                            batch_size=len(packages),
                        ),
                    )
                )

            comment_results = add_timeline_comments_batch(bx, pending_comments)
            for package in packages:
                result = comment_results.get(package["founder_contact_id"])
                if result:
                    package["timeline_comment"].update(result)

    write_batch_csv(args.out, all_rows)
    summary = make_summary(
        packages=packages,
        founder_contact_ids=founder_contact_ids,
        dry_run=dry_run,
        active_manager_ids=config.allowed_user_ids,
        explicit_excluded_manager_ids=explicit_excluded_manager_ids,
        rejected_candidates=rejected_candidates,
        conflicts=conflicts,
    )
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    has_errors = any(
        package["timeline_comment"].get("error")
        or any(text(row.get("error")) for row in package["rows"])
        for package in packages
    )
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
