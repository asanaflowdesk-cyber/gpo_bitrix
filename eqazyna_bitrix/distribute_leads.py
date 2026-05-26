from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .bitrix_client import BitrixClient
from .settings import Settings


LEAD_ORIGINATOR_ID = "EQAZYNA_DIRECTOR_LEAD"
COMPANY_ORIGINATOR_ID = "EQAZYNA"

SOURCE_RESPONSIBLE_ID = 36

ALLOWED_USER_IDS = [
    70, 92, 96, 82, 94, 44, 90,
    72, 74, 76, 78, 80, 84, 86, 88, 98, 100, 102,
]

USER_NAMES = {
    70: "Ольга Скребцова",
    92: "Владислав Тян",
    96: "Сотрудник Тестовый",
    82: "Aisulu Gaisa",
    94: "Марат Шалбаев",
    44: "Sachyova Alyona",
    90: "Асхат Шамар",
    72: "Исаев Асет Саятович",
    74: "Крижевский Андрей Геннадьевич",
    76: "Юлия Сидикова",
    78: "Ксения Кудайбергенова",
    80: "Чернышов Роман Владимирович",
    84: "Петухов Владимир Владимирович",
    86: "Еркебулан Толекбергенов",
    88: "Иманғали Жұмақ",
    98: "Исаев Саги",
    100: "Аралбаев Руслан Турсбекович",
    102: "Қуандық Ринат Русланұлы",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Randomly distribute e-Qazyna Bitrix leads by client limit and sync companies/deals"
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--out", default=None, help="Output JSON log path")
    parser.add_argument("--source-responsible-id", type=int, default=SOURCE_RESPONSIBLE_ID)
    parser.add_argument("--limit-per-manager", type=int, default=15)
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed for reproducible dry-runs")
    return parser.parse_args()


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_bin(raw: str) -> tuple[str | None, str | None]:
    value = _safe_str(raw).strip()

    if not value:
        return None, "empty"

    if "e" in value.lower():
        return None, f"scientific_notation: {value}"

    digits = re.sub(r"\D", "", value)

    if len(digits) != 12:
        return None, f"not_12_digits: {value}"

    return digits, None


def _extract_bins_from_lead(lead: dict[str, Any]) -> list[str]:
    bins: set[str] = set()

    origin_id = _safe_str(lead.get("ORIGIN_ID"))

    if origin_id.startswith("company|"):
        bin_value, error = _normalize_bin(origin_id.replace("company|", "", 1))
        if bin_value and not error:
            bins.add(bin_value)

    comments = _safe_str(lead.get("COMMENTS"))

    for match in re.findall(r"\b\d{12}\b", comments):
        bins.add(match)

    return sorted(bins)


def _package_client_count(lead: dict[str, Any]) -> int:
    """
    Нагрузка считается по клиентам/компаниям, а не по лидам.
    1 БИН = 1 клиент.
    Если БИНов нет, считаем пакет как 1 клиент, чтобы он не был бесплатной нагрузкой.
    """
    bins = _extract_bins_from_lead(lead)
    return max(1, len(bins))


def _is_active_lead(lead: dict[str, Any]) -> bool:
    status = _safe_str(lead.get("STATUS_ID")).upper()
    return status not in {"CONVERTED", "JUNK"}


def _is_active_deal(deal: dict[str, Any]) -> bool:
    return _safe_str(deal.get("CLOSED")).upper() != "Y"


def _list_eqazyna_package_leads(client: BitrixClient) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.lead.list",
        {
            "order": {"ID": "ASC"},
            "filter": {"ORIGINATOR_ID": LEAD_ORIGINATOR_ID},
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "ASSIGNED_BY_ID",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
                "COMMENTS",
            ],
        },
    )


def _list_companies_by_bin(client: BitrixClient, bin_value: str) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": {
                "ORIGINATOR_ID": COMPANY_ORIGINATOR_ID,
                "ORIGIN_ID": bin_value,
            },
            "select": [
                "ID",
                "TITLE",
                "ASSIGNED_BY_ID",
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


def _sync_companies_and_deals(
    client: BitrixClient,
    bins: list[str],
    target_user_id: int,
    dry_run: bool,
) -> dict[str, Any]:
    sync_log: dict[str, Any] = {
        "companies": [],
        "deals": [],
        "missing_companies": [],
        "errors": [],
    }

    seen_company_ids: set[str] = set()
    seen_deal_ids: set[str] = set()

    for bin_value in bins:
        companies = _list_companies_by_bin(client, bin_value)

        if not companies:
            sync_log["missing_companies"].append(
                {
                    "bin": bin_value,
                    "reason": "company_not_found_by_origin_id",
                }
            )
            continue

        for company in companies:
            company_id = str(company.get("ID"))

            if company_id in seen_company_ids:
                continue

            seen_company_ids.add(company_id)

            company_row = {
                "bin": bin_value,
                "company_id": company_id,
                "company_title": company.get("TITLE"),
                "old_assigned_by_id": _to_int(company.get("ASSIGNED_BY_ID")),
                "new_assigned_by_id": target_user_id,
                "action": None,
                "error": None,
            }

            if dry_run:
                company_row["action"] = "dry_run_update_company_responsible"
            else:
                try:
                    _update_company_responsible(client, company_id, target_user_id)
                    company_row["action"] = "updated_company_responsible"
                except Exception as exc:
                    company_row["action"] = "error"
                    company_row["error"] = str(exc)
                    sync_log["errors"].append(company_row)
                    sync_log["companies"].append(company_row)
                    continue

            sync_log["companies"].append(company_row)

            try:
                deals = client.list_deals_by_company(
                    company_id,
                    only_eqazyna=True,
                )
            except Exception as exc:
                sync_log["errors"].append(
                    {
                        "bin": bin_value,
                        "company_id": company_id,
                        "action": "list_deals_error",
                        "error": str(exc),
                    }
                )
                continue

            for deal in deals:
                deal_id = str(deal.get("ID"))

                if deal_id in seen_deal_ids:
                    continue

                seen_deal_ids.add(deal_id)

                deal_row = {
                    "bin": bin_value,
                    "company_id": company_id,
                    "deal_id": deal_id,
                    "deal_title": deal.get("TITLE"),
                    "stage_id": deal.get("STAGE_ID"),
                    "closed": deal.get("CLOSED"),
                    "old_assigned_by_id": _to_int(deal.get("ASSIGNED_BY_ID")),
                    "new_assigned_by_id": target_user_id,
                    "action": None,
                    "error": None,
                }

                if not _is_active_deal(deal):
                    deal_row["action"] = "skip_closed_deal"
                    sync_log["deals"].append(deal_row)
                    continue

                if dry_run:
                    deal_row["action"] = "dry_run_update_deal_responsible"
                else:
                    try:
                        _update_deal_responsible(client, deal_id, target_user_id)
                        deal_row["action"] = "updated_deal_responsible"
                    except Exception as exc:
                        deal_row["action"] = "error"
                        deal_row["error"] = str(exc)
                        sync_log["errors"].append(deal_row)

                sync_log["deals"].append(deal_row)

    return sync_log


def _initial_load(leads: list[dict[str, Any]]) -> dict[int, int]:
    """
    Считаем стартовую нагрузку по клиентам/компаниям внутри активных лидов.
    Один БИН = один клиент.
    """
    load = {user_id: 0 for user_id in ALLOWED_USER_IDS}

    for lead in leads:
        if not _is_active_lead(lead):
            continue

        assigned_id = _to_int(lead.get("ASSIGNED_BY_ID"))

        if assigned_id not in load:
            continue

        load[assigned_id] += _package_client_count(lead)

    return load


def _choose_random_available(
    load: dict[int, int],
    limit_per_manager: int,
    package_client_count: int,
) -> int | None:
    """
    Чистый рандом, но только среди тех, у кого после назначения пакета
    не будет превышения лимита клиентов.
    """
    available_user_ids = [
        user_id
        for user_id in ALLOWED_USER_IDS
        if load.get(user_id, 0) + package_client_count <= limit_per_manager
    ]

    if not available_user_ids:
        return None

    return random.choice(available_user_ids)


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()

    if args.seed is not None:
        random.seed(args.seed)

    if not settings.bitrix_webhook_url:
        raise SystemExit("BITRIX_WEBHOOK_URL is required")

    client = BitrixClient(settings.bitrix_webhook_url, timeout=settings.request_timeout)

    leads = _list_eqazyna_package_leads(client)

    print(f"e-Qazyna package leads found: {len(leads)}")
    print(f"Dry run: {args.dry_run}")
    print(f"Source responsible ID: {args.source_responsible_id}")
    print(f"Allowed users: {len(ALLOWED_USER_IDS)}")
    print(f"Limit per manager, clients: {args.limit_per_manager}")

    load = _initial_load(leads)

    print("Initial client load:")
    for user_id, count in sorted(load.items(), key=lambda item: (item[1], item[0])):
        print(f"  {user_id} {USER_NAMES.get(user_id, '')}: {count}")

    results: list[dict[str, Any]] = []

    summary: dict[str, Any] = {
        "total_leads": len(leads),
        "dry_run": args.dry_run,
        "source_responsible_id": args.source_responsible_id,
        "allowed_user_ids": ALLOWED_USER_IDS,
        "limit_per_manager_clients": args.limit_per_manager,
        "distribution_mode": "pure_random_with_client_limit",
        "seed": args.seed,
    }

    for lead in leads:
        lead_id = str(lead.get("ID"))
        title = _safe_str(lead.get("TITLE"))
        status_id = _safe_str(lead.get("STATUS_ID"))
        assigned_id = _to_int(lead.get("ASSIGNED_BY_ID"))

        row: dict[str, Any] = {
            "lead_id": lead_id,
            "title": title,
            "status_id": status_id,
            "current_assigned_by_id": assigned_id,
            "current_assigned_name": USER_NAMES.get(assigned_id),
            "origin_id": lead.get("ORIGIN_ID"),
            "bins": [],
            "package_client_count": 0,
            "target_user_id": None,
            "target_user_name": None,
            "reason": None,
            "action": None,
            "sync": None,
            "error": None,
        }

        if not _is_active_lead(lead):
            row["action"] = "skip_inactive_status"
            row["reason"] = "lead_status_is_converted_or_junk"
            results.append(row)
            continue

        if assigned_id != args.source_responsible_id:
            row["action"] = "skip_not_source_responsible"
            row["reason"] = f"assigned_by_id_is_{assigned_id}_not_{args.source_responsible_id}"
            results.append(row)
            continue

        bins = _extract_bins_from_lead(lead)
        package_client_count = max(1, len(bins))

        row["bins"] = bins
        row["package_client_count"] = package_client_count

        target_user_id = _choose_random_available(
            load=load,
            limit_per_manager=args.limit_per_manager,
            package_client_count=package_client_count,
        )

        if target_user_id is None:
            row["action"] = "skip_no_capacity"
            row["reason"] = "all_managers_reached_client_limit"
            results.append(row)
            continue

        row["target_user_id"] = target_user_id
        row["target_user_name"] = USER_NAMES.get(target_user_id)
        row["reason"] = "random_with_client_limit"

        if args.dry_run:
            row["action"] = "dry_run_assign_lead"
        else:
            try:
                client.update_lead(
                    lead_id,
                    {
                        "ASSIGNED_BY_ID": target_user_id,
                    },
                )
                row["action"] = "assigned_lead"
            except Exception as exc:
                row["action"] = "error"
                row["error"] = str(exc)
                results.append(row)
                continue

        row["sync"] = _sync_companies_and_deals(
            client=client,
            bins=bins,
            target_user_id=target_user_id,
            dry_run=args.dry_run,
        )

        load[target_user_id] = load.get(target_user_id, 0) + package_client_count
        results.append(row)

    summary["final_planned_client_load"] = {
        str(user_id): {
            "user_name": USER_NAMES.get(user_id),
            "client_load": count,
        }
        for user_id, count in sorted(load.items())
    }

    out = Path(args.out or f"exports/distribute_leads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
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
