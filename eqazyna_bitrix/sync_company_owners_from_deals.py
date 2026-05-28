from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient, BitrixError
from .manager_config import load_manager_config


def _split_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(x) for x in re.split(r"[,;\s]+", raw.strip()) if x.strip()}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:  # noqa: BLE001
        return default


def _sort_key(deal: dict[str, Any]) -> tuple[int, str, str, str]:
    # Bitrix dates are ISO strings, ID is stable fallback. DESC is applied later.
    return (
        _as_int(deal.get("ID")),
        str(deal.get("DATE_MODIFY") or ""),
        str(deal.get("MOVED_TIME") or ""),
        str(deal.get("DATE_CREATE") or ""),
    )


@dataclass
class SyncRow:
    company_id: str
    company_title: str
    old_company_owner_id: str
    old_company_owner_name: str
    target_owner_id: str | None
    target_owner_name: str | None
    action: str
    reason: str
    deal_count: int
    candidate_deal_ids: str
    candidate_owner_ids: str
    conflict_owner_ids: str
    contact_updates: str
    error: str | None = None


class CompanyOwnerSync:
    def __init__(
        self,
        client: BitrixClient,
        source_responsible_ids: set[int],
        allowed_target_ids: set[int],
        user_names: dict[int, str],
        deal_category_id: str | None,
        include_closed_deals: bool,
        only_eqazyna_deals: bool,
        conflict_policy: str,
        sync_contacts: bool,
        dry_run: bool,
        max_companies: int | None,
    ) -> None:
        self.client = client
        self.source_responsible_ids = source_responsible_ids
        self.allowed_target_ids = allowed_target_ids
        self.user_names = user_names
        self.deal_category_id = deal_category_id
        self.include_closed_deals = include_closed_deals
        self.only_eqazyna_deals = only_eqazyna_deals
        self.conflict_policy = conflict_policy
        self.sync_contacts = sync_contacts
        self.dry_run = dry_run
        self.max_companies = max_companies

    def name(self, user_id: int | str | None) -> str:
        uid = _as_int(user_id)
        return self.user_names.get(uid, str(user_id or ""))

    def list_source_companies(self) -> list[dict[str, Any]]:
        companies: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_id in sorted(self.source_responsible_ids):
            items = self.client.list_all(
                "crm.company.list",
                {
                    "order": {"ID": "ASC"},
                    "filter": {"ASSIGNED_BY_ID": source_id},
                    "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "COMMENTS"],
                },
            )
            for item in items:
                company_id = str(item.get("ID") or "")
                if company_id and company_id not in seen:
                    seen.add(company_id)
                    companies.append(item)
                if self.max_companies and len(companies) >= self.max_companies:
                    return companies[: self.max_companies]
        return companies

    def list_company_deals(self, company_id: str) -> list[dict[str, Any]]:
        flt: dict[str, Any] = {"COMPANY_ID": int(company_id)}
        if self.deal_category_id not in (None, "", "all"):
            flt["CATEGORY_ID"] = self.deal_category_id
        if not self.include_closed_deals:
            flt["CLOSED"] = "N"
        if self.only_eqazyna_deals:
            flt["ORIGINATOR_ID"] = "EQAZYNA"
        return self.client.list_all(
            "crm.deal.list",
            {
                "order": {"ID": "DESC"},
                "filter": flt,
                "select": [
                    "ID",
                    "TITLE",
                    "COMPANY_ID",
                    "CATEGORY_ID",
                    "STAGE_ID",
                    "STAGE_SEMANTIC_ID",
                    "CLOSED",
                    "ASSIGNED_BY_ID",
                    "DATE_CREATE",
                    "DATE_MODIFY",
                    "MOVED_TIME",
                    "ORIGINATOR_ID",
                    "ORIGIN_ID",
                ],
            },
        )

    def choose_owner(self, deals: list[dict[str, Any]]) -> tuple[int | None, str, list[dict[str, Any]], list[int]]:
        candidates: list[dict[str, Any]] = []
        for deal in deals:
            owner = _as_int(deal.get("ASSIGNED_BY_ID"))
            if owner in self.source_responsible_ids:
                continue
            if self.allowed_target_ids and owner not in self.allowed_target_ids:
                continue
            candidates.append(deal)

        owners = [_as_int(deal.get("ASSIGNED_BY_ID")) for deal in candidates]
        unique_owners = sorted({owner for owner in owners if owner})
        if not candidates:
            return None, "no_non_technical_deal_owner", candidates, unique_owners
        if len(unique_owners) == 1:
            return unique_owners[0], "single_deal_owner", candidates, unique_owners

        if self.conflict_policy == "latest":
            latest = sorted(candidates, key=_sort_key, reverse=True)[0]
            return _as_int(latest.get("ASSIGNED_BY_ID")), "conflict_latest_deal_owner", candidates, unique_owners

        if self.conflict_policy == "majority":
            counts = Counter(owners)
            most_common = counts.most_common()
            if most_common and len(most_common) >= 2 and most_common[0][1] == most_common[1][1]:
                return None, "conflict_majority_tie", candidates, unique_owners
            if most_common:
                return most_common[0][0], "conflict_majority_deal_owner", candidates, unique_owners

        return None, "conflict_multiple_deal_owners", candidates, unique_owners

    def sync_company_contacts(self, company_id: str, target_owner_id: int) -> list[str]:
        if not self.sync_contacts:
            return []
        updated: list[str] = []
        try:
            contact_ids = self.client.company_contact_ids(company_id)
        except Exception as exc:  # noqa: BLE001
            return [f"contacts_error:{exc}"]
        for contact_id in contact_ids:
            try:
                contact = self.client.call("crm.contact.get", {"id": int(contact_id)})
                old_owner = _as_int((contact or {}).get("ASSIGNED_BY_ID"))
                if old_owner not in self.source_responsible_ids:
                    continue
                if not self.dry_run:
                    self.client.update_contact(str(contact_id), {"ASSIGNED_BY_ID": target_owner_id})
                updated.append(f"{contact_id}:{old_owner}->{target_owner_id}")
            except Exception as exc:  # noqa: BLE001
                updated.append(f"{contact_id}:error:{exc}")
        return updated

    def process_company(self, company: dict[str, Any]) -> SyncRow:
        company_id = str(company.get("ID") or "")
        title = str(company.get("TITLE") or "")
        old_owner = str(company.get("ASSIGNED_BY_ID") or "")
        try:
            deals = self.list_company_deals(company_id)
            target_owner, reason, candidates, conflict_owners = self.choose_owner(deals)
            candidate_deal_ids = ",".join(str(d.get("ID")) for d in candidates)
            candidate_owner_ids = ",".join(str(_as_int(d.get("ASSIGNED_BY_ID"))) for d in candidates)
            conflict_owner_ids = ",".join(str(x) for x in conflict_owners)

            if not target_owner:
                return SyncRow(
                    company_id=company_id,
                    company_title=title,
                    old_company_owner_id=old_owner,
                    old_company_owner_name=self.name(old_owner),
                    target_owner_id=None,
                    target_owner_name=None,
                    action="skipped",
                    reason=reason,
                    deal_count=len(deals),
                    candidate_deal_ids=candidate_deal_ids,
                    candidate_owner_ids=candidate_owner_ids,
                    conflict_owner_ids=conflict_owner_ids,
                    contact_updates="",
                )

            if _as_int(old_owner) == target_owner:
                return SyncRow(
                    company_id=company_id,
                    company_title=title,
                    old_company_owner_id=old_owner,
                    old_company_owner_name=self.name(old_owner),
                    target_owner_id=str(target_owner),
                    target_owner_name=self.name(target_owner),
                    action="already_ok",
                    reason=reason,
                    deal_count=len(deals),
                    candidate_deal_ids=candidate_deal_ids,
                    candidate_owner_ids=candidate_owner_ids,
                    conflict_owner_ids=conflict_owner_ids,
                    contact_updates="",
                )

            contact_updates = self.sync_company_contacts(company_id, target_owner)
            if not self.dry_run:
                self.client.update_company(company_id, {"ASSIGNED_BY_ID": target_owner})
                self.client.add_timeline_comment(
                    "company",
                    company_id,
                    (
                        "Автоисправление ответственного компании по ответственному в сделках e-Qazyna.\n"
                        f"Было: {old_owner} / {self.name(old_owner)}\n"
                        f"Стало: {target_owner} / {self.name(target_owner)}\n"
                        f"Основание: {reason}\n"
                        f"Сделки-основания: {candidate_deal_ids or '-'}"
                    ),
                )

            return SyncRow(
                company_id=company_id,
                company_title=title,
                old_company_owner_id=old_owner,
                old_company_owner_name=self.name(old_owner),
                target_owner_id=str(target_owner),
                target_owner_name=self.name(target_owner),
                action="dry_run_update" if self.dry_run else "updated",
                reason=reason,
                deal_count=len(deals),
                candidate_deal_ids=candidate_deal_ids,
                candidate_owner_ids=candidate_owner_ids,
                conflict_owner_ids=conflict_owner_ids,
                contact_updates=";".join(contact_updates),
            )
        except Exception as exc:  # noqa: BLE001
            return SyncRow(
                company_id=company_id,
                company_title=title,
                old_company_owner_id=old_owner,
                old_company_owner_name=self.name(old_owner),
                target_owner_id=None,
                target_owner_name=None,
                action="error",
                reason="exception",
                deal_count=0,
                candidate_deal_ids="",
                candidate_owner_ids="",
                conflict_owner_ids="",
                contact_updates="",
                error=str(exc),
            )

    def run(self) -> list[SyncRow]:
        companies = self.list_source_companies()
        return [self.process_company(company) for company in companies]


def write_outputs(rows: list[SyncRow], json_out: Path, csv_out: Path) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    data = [asdict(row) for row in rows]
    json_out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(data[0].keys()) if data else list(SyncRow.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-time sync of company responsible users from assigned e-Qazyna deals.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--source-responsible-ids", default="36,44", help="Technical/current responsible IDs to repair")
    parser.add_argument("--deal-category-id", default="0", help="Deal category to inspect. Use 'all' to inspect all categories")
    parser.add_argument("--include-closed-deals", action="store_true", help="Use closed deals too when choosing company owner")
    parser.add_argument("--include-non-eqazyna-deals", action="store_true", help="Use all company deals, not only ORIGINATOR_ID=EQAZYNA")
    parser.add_argument("--conflict-policy", choices=["skip", "latest", "majority"], default="skip", help="What to do if one company has deals assigned to different managers")
    parser.add_argument("--sync-contacts", action="store_true", help="Also move linked contacts if they are assigned to source IDs")
    parser.add_argument("--max-companies", type=int, default=0, help="Optional safety limit for test runs")
    parser.add_argument("--out", default="exports/sync_company_owners_from_deals_log.json")
    parser.add_argument("--csv-out", default="exports/sync_company_owners_from_deals_log.csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    webhook_url = os.getenv("BITRIX_WEBHOOK_URL", "")
    timeout = int(os.getenv("REQUEST_TIMEOUT", "60"))

    cfg = load_manager_config()
    source_ids = _split_ids(args.source_responsible_ids) or set(cfg.source_responsible_ids)
    allowed_ids = set(cfg.allowed_user_ids)
    user_names = cfg.user_names

    if not args.dry_run and not webhook_url:
        raise SystemExit("ERROR: BITRIX_WEBHOOK_URL is empty. Cannot write to Bitrix.")

    client = BitrixClient(webhook_url=webhook_url, timeout=timeout)
    sync = CompanyOwnerSync(
        client=client,
        source_responsible_ids=source_ids,
        allowed_target_ids=allowed_ids,
        user_names=user_names,
        deal_category_id=args.deal_category_id,
        include_closed_deals=args.include_closed_deals,
        only_eqazyna_deals=not args.include_non_eqazyna_deals,
        conflict_policy=args.conflict_policy,
        sync_contacts=args.sync_contacts,
        dry_run=args.dry_run,
        max_companies=args.max_companies or None,
    )
    rows = sync.run()
    write_outputs(rows, Path(args.out), Path(args.csv_out))

    counts = Counter(row.action for row in rows)
    print("SYNC_COMPANY_OWNERS_FROM_DEALS_DONE")
    print(f"dry_run={args.dry_run}")
    print(f"companies_checked={len(rows)}")
    for action, count in sorted(counts.items()):
        print(f"{action}={count}")
    errors = [row for row in rows if row.error]
    if errors:
        print(f"errors={len(errors)}")
        return 2 if not args.dry_run else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
