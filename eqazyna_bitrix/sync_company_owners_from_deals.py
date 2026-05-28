from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .manager_config import load_manager_config


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:  # noqa: BLE001
        return default


def _split_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in str(raw).replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


@dataclass
class DealAuditRow:
    company_id: str
    company_title: str
    company_owner_id: str
    company_owner_name: str
    deal_id: str
    deal_title: str
    deal_owner_id: str
    deal_owner_name: str
    mismatch: str
    company_owner_is_source: str
    deal_owner_is_source: str
    deal_owner_is_allowed_target: str
    category_id: str
    stage_id: str
    stage_semantic_id: str
    closed: str
    date_create: str
    date_modify: str
    moved_time: str
    originator_id: str
    origin_id: str
    deal_url_hint: str


@dataclass
class CompanyMismatchSummaryRow:
    company_id: str
    company_title: str
    company_owner_id: str
    company_owner_name: str
    deal_owner_ids: str
    deal_owner_names: str
    deal_count: int
    mismatch_deal_count: int
    mismatch_deal_ids: str
    mismatch_deal_titles: str
    recommended_owner_id: str
    recommended_owner_name: str
    recommendation_reason: str


class CompanyDealOwnerAudit:
    def __init__(
        self,
        client: BitrixClient,
        user_names: dict[int, str],
        source_ids: set[int],
        allowed_target_ids: set[int],
        scope: str,
        deal_category_id: str | None,
        include_closed_deals: bool,
        only_eqazyna_deals: bool,
        max_companies: int | None,
    ) -> None:
        self.client = client
        self.user_names = user_names
        self.source_ids = source_ids
        self.allowed_target_ids = allowed_target_ids
        self.scope = scope
        self.deal_category_id = deal_category_id
        self.include_closed_deals = include_closed_deals
        self.only_eqazyna_deals = only_eqazyna_deals
        self.max_companies = max_companies

    def name(self, user_id: int | str | None) -> str:
        uid = _as_int(user_id)
        return self.user_names.get(uid, str(user_id or ''))

    def list_companies(self) -> list[dict[str, Any]]:
        if self.scope == 'source_companies':
            companies: list[dict[str, Any]] = []
            seen: set[str] = set()
            for source_id in sorted(self.source_ids):
                batch = self.client.list_all(
                    'crm.company.list',
                    {
                        'order': {'ID': 'ASC'},
                        'filter': {'ASSIGNED_BY_ID': source_id},
                        'select': ['ID', 'TITLE', 'ASSIGNED_BY_ID', 'ORIGINATOR_ID', 'ORIGIN_ID'],
                    },
                )
                for item in batch:
                    cid = str(item.get('ID') or '')
                    if cid and cid not in seen:
                        seen.add(cid)
                        companies.append(item)
                    if self.max_companies and len(companies) >= self.max_companies:
                        return companies[: self.max_companies]
            return companies

        companies = self.client.list_all(
            'crm.company.list',
            {
                'order': {'ID': 'ASC'},
                'filter': {},
                'select': ['ID', 'TITLE', 'ASSIGNED_BY_ID', 'ORIGINATOR_ID', 'ORIGIN_ID'],
            },
            limit=self.max_companies,
        )
        return companies

    def list_company_deals(self, company_id: str) -> list[dict[str, Any]]:
        flt: dict[str, Any] = {'COMPANY_ID': int(company_id)}
        if self.deal_category_id not in (None, '', 'all'):
            flt['CATEGORY_ID'] = self.deal_category_id
        if not self.include_closed_deals:
            flt['CLOSED'] = 'N'
        if self.only_eqazyna_deals:
            flt['ORIGINATOR_ID'] = 'EQAZYNA'
        return self.client.list_all(
            'crm.deal.list',
            {
                'order': {'ID': 'DESC'},
                'filter': flt,
                'select': [
                    'ID',
                    'TITLE',
                    'COMPANY_ID',
                    'CATEGORY_ID',
                    'STAGE_ID',
                    'STAGE_SEMANTIC_ID',
                    'CLOSED',
                    'ASSIGNED_BY_ID',
                    'DATE_CREATE',
                    'DATE_MODIFY',
                    'MOVED_TIME',
                    'ORIGINATOR_ID',
                    'ORIGIN_ID',
                ],
            },
        )

    def build_recommendation(self, company_owner: int, deals: list[dict[str, Any]]) -> tuple[str, str, str]:
        owners: list[int] = []
        for deal in deals:
            owner = _as_int(deal.get('ASSIGNED_BY_ID'))
            if not owner:
                continue
            if owner in self.source_ids:
                continue
            if self.allowed_target_ids and owner not in self.allowed_target_ids:
                continue
            owners.append(owner)
        unique = sorted(set(owners))
        if not unique:
            return '', '', 'no_non_technical_deal_owner'
        if len(unique) == 1:
            owner = unique[0]
            if owner == company_owner:
                return str(owner), self.name(owner), 'already_same_as_company_owner'
            return str(owner), self.name(owner), 'single_non_technical_deal_owner'
        counts: dict[int, int] = {}
        for owner in owners:
            counts[owner] = counts.get(owner, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
            return '', '', 'conflict_tie_between_deal_owners'
        owner = ranked[0][0]
        return str(owner), self.name(owner), 'majority_non_technical_deal_owner'

    def run(self) -> tuple[list[DealAuditRow], list[DealAuditRow], list[CompanyMismatchSummaryRow]]:
        audit_rows: list[DealAuditRow] = []
        mismatch_rows: list[DealAuditRow] = []
        summary_rows: list[CompanyMismatchSummaryRow] = []

        for company in self.list_companies():
            company_id = str(company.get('ID') or '')
            if not company_id:
                continue
            company_owner = _as_int(company.get('ASSIGNED_BY_ID'))
            company_title = str(company.get('TITLE') or '')
            deals = self.list_company_deals(company_id)
            if not deals:
                continue

            company_mismatch_rows: list[DealAuditRow] = []
            for deal in deals:
                deal_id = str(deal.get('ID') or '')
                deal_owner = _as_int(deal.get('ASSIGNED_BY_ID'))
                mismatch = company_owner != deal_owner
                row = DealAuditRow(
                    company_id=company_id,
                    company_title=company_title,
                    company_owner_id=str(company_owner or ''),
                    company_owner_name=self.name(company_owner),
                    deal_id=deal_id,
                    deal_title=str(deal.get('TITLE') or ''),
                    deal_owner_id=str(deal_owner or ''),
                    deal_owner_name=self.name(deal_owner),
                    mismatch='Y' if mismatch else 'N',
                    company_owner_is_source='Y' if company_owner in self.source_ids else 'N',
                    deal_owner_is_source='Y' if deal_owner in self.source_ids else 'N',
                    deal_owner_is_allowed_target='Y' if (not self.allowed_target_ids or deal_owner in self.allowed_target_ids) else 'N',
                    category_id=str(deal.get('CATEGORY_ID') or ''),
                    stage_id=str(deal.get('STAGE_ID') or ''),
                    stage_semantic_id=str(deal.get('STAGE_SEMANTIC_ID') or ''),
                    closed=str(deal.get('CLOSED') or ''),
                    date_create=str(deal.get('DATE_CREATE') or ''),
                    date_modify=str(deal.get('DATE_MODIFY') or ''),
                    moved_time=str(deal.get('MOVED_TIME') or ''),
                    originator_id=str(deal.get('ORIGINATOR_ID') or ''),
                    origin_id=str(deal.get('ORIGIN_ID') or ''),
                    deal_url_hint=f'/crm/deal/details/{deal_id}/' if deal_id else '',
                )
                audit_rows.append(row)
                if mismatch:
                    mismatch_rows.append(row)
                    company_mismatch_rows.append(row)

            if company_mismatch_rows:
                deal_owner_ids = sorted({_as_int(row.deal_owner_id) for row in company_mismatch_rows if _as_int(row.deal_owner_id)})
                recommended_id, recommended_name, reason = self.build_recommendation(company_owner, deals)
                summary_rows.append(
                    CompanyMismatchSummaryRow(
                        company_id=company_id,
                        company_title=company_title,
                        company_owner_id=str(company_owner or ''),
                        company_owner_name=self.name(company_owner),
                        deal_owner_ids=', '.join(str(x) for x in deal_owner_ids),
                        deal_owner_names=', '.join(self.name(x) for x in deal_owner_ids),
                        deal_count=len(deals),
                        mismatch_deal_count=len(company_mismatch_rows),
                        mismatch_deal_ids=', '.join(row.deal_id for row in company_mismatch_rows),
                        mismatch_deal_titles=' | '.join(row.deal_title for row in company_mismatch_rows),
                        recommended_owner_id=recommended_id,
                        recommended_owner_name=recommended_name,
                        recommendation_reason=reason,
                    )
                )

        return audit_rows, mismatch_rows, summary_rows


def _write_csv(path: Path, rows: list[Any], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([asdict(row) for row in rows])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Audit companies whose responsible user differs from linked deal responsible users.')
    parser.add_argument('--scope', choices=['all_companies', 'source_companies'], default='all_companies')
    parser.add_argument('--source-responsible-ids', default='36,44')
    parser.add_argument('--deal-category-id', default='all', help="Deal category to inspect. Use 'all' for all categories")
    parser.add_argument('--include-closed-deals', action='store_true')
    parser.add_argument('--include-non-eqazyna-deals', action='store_true')
    parser.add_argument('--max-companies', type=int, default=0)
    parser.add_argument('--out', default='exports/company_deal_owner_audit.json')
    parser.add_argument('--csv-out', default='exports/company_deal_owner_audit.csv')
    parser.add_argument('--mismatch-out', default='exports/company_deal_owner_mismatches.json')
    parser.add_argument('--mismatch-csv-out', default='exports/company_deal_owner_mismatches.csv')
    parser.add_argument('--summary-out', default='exports/company_deal_owner_mismatch_summary.json')
    parser.add_argument('--summary-csv-out', default='exports/company_deal_owner_mismatch_summary.csv')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    webhook_url = os.getenv('BITRIX_WEBHOOK_URL', '')
    timeout = int(os.getenv('REQUEST_TIMEOUT', '60'))
    if not webhook_url:
        raise SystemExit('ERROR: BITRIX_WEBHOOK_URL is empty.')

    cfg = load_manager_config()
    source_ids = _split_ids(args.source_responsible_ids) or set(cfg.source_responsible_ids)
    audit = CompanyDealOwnerAudit(
        client=BitrixClient(webhook_url=webhook_url, timeout=timeout),
        user_names=cfg.user_names,
        source_ids=source_ids,
        allowed_target_ids=set(cfg.allowed_user_ids),
        scope=args.scope,
        deal_category_id=args.deal_category_id,
        include_closed_deals=args.include_closed_deals,
        only_eqazyna_deals=not args.include_non_eqazyna_deals,
        max_companies=args.max_companies or None,
    )
    all_rows, mismatch_rows, summary_rows = audit.run()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps([asdict(row) for row in all_rows], ensure_ascii=False, indent=2), encoding='utf-8')
    Path(args.mismatch_out).write_text(json.dumps([asdict(row) for row in mismatch_rows], ensure_ascii=False, indent=2), encoding='utf-8')
    Path(args.summary_out).write_text(json.dumps([asdict(row) for row in summary_rows], ensure_ascii=False, indent=2), encoding='utf-8')

    _write_csv(Path(args.csv_out), all_rows, list(DealAuditRow.__dataclass_fields__.keys()))
    _write_csv(Path(args.mismatch_csv_out), mismatch_rows, list(DealAuditRow.__dataclass_fields__.keys()))
    _write_csv(Path(args.summary_csv_out), summary_rows, list(CompanyMismatchSummaryRow.__dataclass_fields__.keys()))

    print('COMPANY_DEAL_OWNER_AUDIT_DONE')
    print(f'scope={args.scope}')
    print(f'all_deal_rows={len(all_rows)}')
    print(f'mismatch_deal_rows={len(mismatch_rows)}')
    print(f'mismatch_companies={len(summary_rows)}')
    print(f'mismatch_csv={args.mismatch_csv_out}')
    print(f'summary_csv={args.summary_csv_out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
