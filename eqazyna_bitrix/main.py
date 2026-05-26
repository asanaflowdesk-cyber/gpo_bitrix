from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from .bitrix_client import BitrixClient
from .egov_client import EgovClient
from .exporter import write_xlsx
from .models import ProcessResult, CompanyEnrichment
from .pipeline import BitrixPipeline, BitrixPipelineConfig
from .scraper import EqazynaScraper
from .settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="e-Qazyna → eGov → Bitrix24 companies/deals")
    parser.add_argument("--pages", type=int, default=int(os.getenv("EQAZYNA_PAGES", "2")), help="How many pages to process in this run")
    parser.add_argument("--page-start", type=int, default=int(os.getenv("EQAZYNA_PAGE_START", "1")), help="First e-Qazyna page for this run")
    parser.add_argument("--page-list", default=os.getenv("EQAZYNA_PAGE_LIST") or None, help="Optional explicit pages/ranges, e.g. 16,22,30-35. Overrides page-start/pages.")
    parser.add_argument("--doc-type", default=os.getenv("EQAZYNA_DOC_TYPE", "Заявка на разведку ТПИ"))
    parser.add_argument("--statuses", default=os.getenv("EQAZYNA_STATUSES", "Отправлено на рассмотрение,Принято"))
    parser.add_argument("--min-created-date", default=os.getenv("EQAZYNA_MIN_CREATED_DATE") or None, help="Only process applications created on/after YYYY-MM-DD")
    parser.add_argument("--out", default=None, help="Output XLSX path")
    parser.add_argument("--json-out", default=None, help="Optional JSON log path")
    parser.add_argument("--no-egov", action="store_true", help="Do not call data.egov.kz")
    parser.add_argument("--push-bitrix", action="store_true", help="Create/update Bitrix companies and deals")
    parser.add_argument("--dry-run", action="store_true", help="Do everything except Bitrix writes")
    parser.add_argument("--crm-mode", choices=["deal", "lead"], default=os.getenv("BITRIX_CRM_MODE", "deal"), help="deal = old company+deal flow; lead = create/update Bitrix leads only")
    parser.add_argument("--deal-category-id", default=os.getenv("BITRIX_DEAL_CATEGORY_ID", "0"))
    parser.add_argument("--deal-stage-id", default=os.getenv("BITRIX_DEAL_STAGE_ID", "NEW"))
    parser.add_argument("--lead-status-id", default=os.getenv("BITRIX_LEAD_STATUS_ID", "NEW"))
    parser.add_argument("--assigned-by-id", default=os.getenv("BITRIX_ASSIGNED_BY_ID") or None)
    parser.add_argument("--assignment-limit-per-manager", type=int, default=int(os.getenv("BITRIX_ASSIGNMENT_LIMIT_PER_MANAGER", "15")))
    parser.add_argument("--requisite-preset-id", default=os.getenv("BITRIX_REQUISITE_PRESET_ID") or None)
    parser.add_argument("--requisite-bin-field", default=os.getenv("BITRIX_REQUISITE_BIN_FIELD", "RQ_BIN"))
    parser.add_argument("--strict-page-errors", action="store_true", help="Fail the whole run if any e-Qazyna page fails")
    parser.add_argument("--max-consecutive-page-errors", type=int, default=int(os.getenv("EQAZYNA_MAX_CONSECUTIVE_PAGE_ERRORS", "5")), help="Stop scraping after N consecutive failed pages and process already collected rows. Use 0 to never stop early.")
    return parser.parse_args()


def _enrichment_key(bin_number: str, name: str) -> tuple[str, str]:
    return ((bin_number or "").strip(), " ".join((name or "").lower().split()))


def _build_enrichment_map(applications, egov: EgovClient) -> dict[tuple[str, str], CompanyEnrichment]:
    """Stage eGov enrichment before Bitrix writes.

    This prevents the slow and inconsistent pattern "application → eGov → Bitrix"
    for every single row. We enrich unique BIN+applicant-name pairs first, then
    reuse the same result for all applications with that pair.
    """
    unique: dict[tuple[str, str], tuple[str, str]] = {}
    for app in applications:
        key = _enrichment_key(app.bin, app.applicant_name)
        unique.setdefault(key, (app.bin, app.applicant_name))

    print(f"    unique eGov BIN+name pairs: {len(unique)} from {len(applications)} applications")
    result: dict[tuple[str, str], CompanyEnrichment] = {}
    for idx, (key, (bin_number, name)) in enumerate(unique.items(), start=1):
        print(f"    eGov {idx}/{len(unique)} {bin_number} {name[:70]}")
        result[key] = egov.get_company(bin_number, name)
    return result


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = args.out or f"exports/eqazyna_bitrix_log_{timestamp}.xlsx"
    json_path = args.json_out or f"exports/eqazyna_bitrix_log_{timestamp}.json"

    min_created_date = None
    if args.min_created_date:
        min_created_date = datetime.strptime(args.min_created_date, "%Y-%m-%d").date()

    print(f"[1/4] Scraping e-Qazyna: page_start={args.page_start}, pages={args.pages}, page_list={args.page_list!r}, max_consecutive_page_errors={args.max_consecutive_page_errors}, doc_type={args.doc_type!r}, statuses={statuses}, min_created_date={min_created_date}")
    scraper = EqazynaScraper(
        timeout=settings.request_timeout,
        polite_delay_seconds=settings.polite_delay_seconds,
        continue_on_page_error=not args.strict_page_errors,
        max_consecutive_page_errors=args.max_consecutive_page_errors,
    )
    applications = scraper.scrape(
        args.pages,
        args.doc_type,
        statuses,
        min_created_date=min_created_date,
        page_start=args.page_start,
        page_list=args.page_list,
    )
    print(f"Found applications after filter: {len(applications)}")
    if scraper.failed_pages:
        print(f"FAILED_PAGES={','.join(map(str, scraper.failed_pages))}")

    egov = EgovClient(None if args.no_egov else settings.egov_api_key, timeout=settings.request_timeout)

    bitrix_pipeline: BitrixPipeline | None = None
    if args.push_bitrix:
        if not settings.bitrix_webhook_url:
            raise SystemExit("BITRIX_WEBHOOK_URL is required when --push-bitrix is used")
        bitrix_pipeline = BitrixPipeline(
            BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout),
            BitrixPipelineConfig(
                crm_mode=args.crm_mode,
                deal_category_id=args.deal_category_id,
                deal_stage_id=args.deal_stage_id,
                lead_status_id=args.lead_status_id,
                assigned_by_id=args.assigned_by_id,
                requisite_preset_id=args.requisite_preset_id,
                requisite_bin_field=args.requisite_bin_field,
                dry_run=args.dry_run,
                assignment_limit_per_manager=args.assignment_limit_per_manager,
            ),
        )

    results: list[ProcessResult] = []
    print("[2/4] Staged eGov enrichment and Bitrix processing")
    enrichment_map = _build_enrichment_map(applications, egov) if applications else {}

    for idx, app in enumerate(applications, start=1):
        print(f"  Bitrix {idx}/{len(applications)} {app.doc_number} {app.bin} {app.applicant_name[:60]}")
        enrichment = enrichment_map.get(_enrichment_key(app.bin, app.applicant_name)) or CompanyEnrichment(bin=app.bin, error="enrichment_missing")
        if bitrix_pipeline:
            result = bitrix_pipeline.process(app, enrichment)
        else:
            result = ProcessResult(app, enrichment, action="excel_only")
        if result.error:
            print(f"    ERROR: {result.error}")
        else:
            print(f"    {result.action}: company={result.company_id} deal={result.deal_id} lead={result.lead_id}")
        results.append(result)

    print("[3/4] Writing logs")
    xlsx = write_xlsx(results, xlsx_path)
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=2), encoding="utf-8")

    pages_path = Path(json_path).with_name(Path(json_path).stem + "_pages.json")
    pages_payload = {
        "page_start": args.page_start,
        "pages_requested": args.pages,
        "page_list": args.page_list,
        "failed_pages": scraper.failed_pages,
        "page_logs": [p.as_dict() for p in scraper.page_logs],
        "applications_collected": len(applications),
        "results_written": len(results),
    }
    pages_path.write_text(json.dumps(pages_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"XLSX: {xlsx}")
    print(f"PAGES JSON: {pages_path}")
    print(f"JSON: {json_path}")
    print("[4/4] Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
