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
    parser.add_argument("--pages", type=int, default=int(os.getenv("EQAZYNA_PAGES", "3")), help="How many pages to process in this run")
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
    parser.add_argument("--assignment-limit-per-manager", type=int, default=int(os.getenv("BITRIX_ASSIGNMENT_LIMIT_PER_MANAGER", "30")), help="Soft deal limit for brand-new director packages. Historical director packages ignore the limit.")
    parser.add_argument("--assignment-load-stage-ids", default=os.getenv("BITRIX_ASSIGNMENT_LOAD_STAGE_IDS", "NEW,EXECUTING"), help="Comma-separated STAGE_ID values that consume the assignment limit. Default: NEW,EXECUTING. Other stages do not count.")
    parser.add_argument("--inherit-failed-deals-by-director", default=os.getenv("BITRIX_INHERIT_FAILED_DEALS_BY_DIRECTOR", "true"), help="true = new deals inherit failed final stage/reason from old deals for the same director")
    parser.add_argument("--failed-deal-stage-ids", default=os.getenv("BITRIX_FAILED_DEAL_STAGE_IDS", "LOSE"), help="Comma-separated failed deal STAGE_ID values. STAGE_SEMANTIC_ID=F is also treated as failed when returned by Bitrix.")
    parser.add_argument("--failed-deal-reason-fields", default=os.getenv("BITRIX_FAILED_DEAL_REASON_FIELDS", "UF_CRM_1779448756033"), help="Comma-separated deal fields to copy/read as failure reason")
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
                assignment_load_stage_ids=args.assignment_load_stage_ids,
                inherit_failed_deals_by_director=str(args.inherit_failed_deals_by_director).lower() == "true",
                failed_deal_stage_ids=args.failed_deal_stage_ids,
                failed_deal_reason_fields=args.failed_deal_reason_fields,
            ),
        )

    results: list[ProcessResult] = []
    print("[2/4] Existing-deal precheck, staged eGov enrichment and Bitrix processing")

    existing_deal_results: dict[str, ProcessResult] = {}
    applications_for_enrichment: list = []
    if bitrix_pipeline and (args.crm_mode or "deal").lower() == "deal":
        for app in applications:
            try:
                existing_deal = bitrix_pipeline.client.find_deal_by_origin(app.application_key)
            except Exception as exc:  # noqa: BLE001
                existing_deal_results[app.application_key] = ProcessResult(
                    app,
                    CompanyEnrichment(bin=app.bin),
                    action="existing_deal_precheck_error",
                    error=str(exc),
                )
                continue
            if existing_deal:
                assigned_by_id = bitrix_pipeline._record_assigned_by_id(existing_deal)
                existing_deal_results[app.application_key] = ProcessResult(
                    app,
                    CompanyEnrichment(
                        bin=app.bin,
                        match_reason="eGov skipped: Bitrix deal already exists by ORIGIN_ID/application_key",
                    ),
                    action="existing_deal_skipped",
                    company_id=str(existing_deal.get("COMPANY_ID") or "") or None,
                    deal_id=str(existing_deal.get("ID") or "") or None,
                    assigned_by_id=assigned_by_id,
                    assigned_by_name=bitrix_pipeline._user_name(assigned_by_id),
                    assignment_reason="existing_deal_no_update_precheck",
                )
            else:
                applications_for_enrichment.append(app)
        print(
            f"    existing Bitrix deals skipped before eGov: {len(existing_deal_results)}; "
            f"applications sent to eGov: {len(applications_for_enrichment)}"
        )
    else:
        applications_for_enrichment = list(applications)

    enrichment_map = _build_enrichment_map(applications_for_enrichment, egov) if applications_for_enrichment else {}

    for idx, app in enumerate(applications, start=1):
        print(f"  Bitrix {idx}/{len(applications)} {app.doc_number} {app.bin} {app.applicant_name[:60]}")
        prechecked_result = existing_deal_results.get(app.application_key)
        if prechecked_result:
            result = prechecked_result
        else:
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
