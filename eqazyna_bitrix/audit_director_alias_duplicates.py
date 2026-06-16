from __future__ import annotations

import argparse
import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .bitrix_client import BitrixClient
from .director import clean_director_value, extract_director_from_text, normalize_fio

LEGAL_WORDS_RE = re.compile(
    r"\b(ТОО|ИП|ЖШС|LLP|LTD|LIMITED|COMPANY|КОМПАНИЯ|GOLD|MINING|GROUP|INC|CO|KAZAKHSTAN|KAZ|ТАУ|КЕН|РУД|РУДА|МЕТАЛЛ|ТУНГСТЕН|TUNGSTEN)\b",
    flags=re.IGNORECASE,
)

TOKEN_RE = re.compile(r"[A-ZА-ЯЁ]+", flags=re.IGNORECASE)


def normalize_person_text(value: str | None) -> str:
    cleaned = clean_director_value(value)
    if not cleaned:
        return ""
    normalized = normalize_fio(cleaned)
    normalized = normalized.replace(".", " ")
    normalized = re.sub(r"[^A-ZА-ЯЁ\s-]+", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def person_tokens(value: str | None) -> list[str]:
    normalized = normalize_person_text(value)
    if not normalized:
        return []
    return [t for t in TOKEN_RE.findall(normalized) if t]


def looks_like_person(value: str | None) -> bool:
    normalized = normalize_person_text(value)
    if not normalized:
        return False
    if LEGAL_WORDS_RE.search(normalized):
        return False
    tokens = person_tokens(normalized)
    if len(tokens) < 2:
        return False
    # At least one surname-like token and one name/initial-like token.
    return any(len(t) > 1 for t in tokens)


def key_from_parts(last_name: str, first_name_or_initial: str) -> str:
    last_name = normalize_fio(last_name).replace(".", "").strip()
    first = normalize_fio(first_name_or_initial).replace(".", "").strip()
    if not last_name or not first:
        return ""
    return f"{last_name}|{first[0]}"


def alias_keys_for_value(value: str | None, *, allow_reversed_two_words: bool = False) -> list[str]:
    """Return possible alias keys like ЛЯБАХ|Г.

    The main case is LAST_NAME + first name initial. For company titles / messy data,
    two-word values may be FIRST LAST, so allow an additional reversed key.
    """
    if not looks_like_person(value):
        return []
    tokens = person_tokens(value)
    keys: list[str] = []

    # LAST INITIAL / LAST FIRST / LAST FIRST PATRONYMIC
    if len(tokens) >= 2 and len(tokens[0]) > 1:
        k = key_from_parts(tokens[0], tokens[1])
        if k:
            keys.append(k)

    # INITIAL LAST / FIRST LAST fallback for messy company titles.
    if allow_reversed_two_words and len(tokens) == 2 and len(tokens[1]) > 1:
        k = key_from_parts(tokens[1], tokens[0])
        if k:
            keys.append(k)

    # F. LAST / G. LYABAKH
    if len(tokens) >= 2 and len(tokens[0]) == 1 and len(tokens[1]) > 1:
        k = key_from_parts(tokens[1], tokens[0])
        if k:
            keys.append(k)

    # Deduplicate preserving order.
    out: list[str] = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def best_display_name(values: Iterable[str]) -> str:
    items = [v for v in values if v]
    if not items:
        return ""
    # Prefer the most complete value: more tokens, longer text.
    return sorted(items, key=lambda x: (len(person_tokens(x)), len(x)), reverse=True)[0]


@dataclass(slots=True)
class AliasRecord:
    alias_key: str
    source_type: str
    source_id: str
    title: str
    raw_name: str
    normalized_name: str
    contact_id: str
    company_id: str
    deal_id: str
    owner_id: str
    stage_id: str
    closed: str
    origin_id: str
    url_hint: str


def list_contacts(client: BitrixClient, limit: int | None = None) -> list[dict[str, Any]]:
    return client.list_all(
        "crm.contact.list",
        {
            "order": {"ID": "ASC"},
            "filter": {},
            "select": ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "ASSIGNED_BY_ID", "COMPANY_ID", "POST", "COMMENTS"],
        },
        limit=limit,
    )


def list_companies(client: BitrixClient, only_eqazyna: bool, limit: int | None = None) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {}
    if only_eqazyna:
        flt["ORIGINATOR_ID"] = "EQAZYNA"
    return client.list_all(
        "crm.company.list",
        {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": ["ID", "TITLE", "COMMENTS", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID"],
        },
        limit=limit,
    )


def list_deals(client: BitrixClient, only_eqazyna: bool, include_closed: bool, limit: int | None = None) -> list[dict[str, Any]]:
    flt: dict[str, Any] = {}
    if only_eqazyna:
        flt["ORIGINATOR_ID"] = "EQAZYNA"
    if not include_closed:
        flt["CLOSED"] = "N"
    return client.list_all(
        "crm.deal.list",
        {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": [
                "ID",
                "TITLE",
                "CONTACT_ID",
                "COMPANY_ID",
                "COMMENTS",
                "ASSIGNED_BY_ID",
                "STAGE_ID",
                "CLOSED",
                "ORIGINATOR_ID",
                "ORIGIN_ID",
            ],
        },
        limit=limit,
    )


def add_record(records: list[AliasRecord], *, alias_key: str, source_type: str, source_id: str, title: str = "", raw_name: str = "", contact_id: str = "", company_id: str = "", deal_id: str = "", owner_id: str = "", stage_id: str = "", closed: str = "", origin_id: str = "") -> None:
    records.append(
        AliasRecord(
            alias_key=alias_key,
            source_type=source_type,
            source_id=str(source_id or ""),
            title=str(title or ""),
            raw_name=str(raw_name or ""),
            normalized_name=normalize_person_text(raw_name),
            contact_id=str(contact_id or ""),
            company_id=str(company_id or ""),
            deal_id=str(deal_id or ""),
            owner_id=str(owner_id or ""),
            stage_id=str(stage_id or ""),
            closed=str(closed or ""),
            origin_id=str(origin_id or ""),
            url_hint=f"/crm/{'deal' if deal_id else 'contact' if contact_id else 'company'}/details/{deal_id or contact_id or company_id}/",
        )
    )


def collect_records(client: BitrixClient, *, include_contacts: bool, include_companies: bool, include_deals: bool, only_eqazyna: bool, include_closed_deals: bool, max_contacts: int, max_companies: int, max_deals: int) -> list[AliasRecord]:
    records: list[AliasRecord] = []

    if include_contacts:
        for contact in list_contacts(client, limit=max_contacts or None):
            fio = " ".join(
                part
                for part in [
                    str(contact.get("LAST_NAME") or "").strip(),
                    str(contact.get("NAME") or "").strip(),
                    str(contact.get("SECOND_NAME") or "").strip(),
                ]
                if part
            )
            for key in alias_keys_for_value(fio):
                add_record(
                    records,
                    alias_key=key,
                    source_type="contact_fio",
                    source_id=str(contact.get("ID") or ""),
                    title=fio,
                    raw_name=fio,
                    contact_id=str(contact.get("ID") or ""),
                    company_id=str(contact.get("COMPANY_ID") or ""),
                    owner_id=str(contact.get("ASSIGNED_BY_ID") or ""),
                )

    if include_companies:
        for company in list_companies(client, only_eqazyna=only_eqazyna, limit=max_companies or None):
            company_id = str(company.get("ID") or "")
            title = str(company.get("TITLE") or "")
            # Person-like company title, e.g. ЛЯБАХ Г.Г. / ГЕННАДИЙ ЛЯБАХ.
            for key in alias_keys_for_value(title, allow_reversed_two_words=True):
                add_record(
                    records,
                    alias_key=key,
                    source_type="company_title",
                    source_id=company_id,
                    title=title,
                    raw_name=title,
                    company_id=company_id,
                    owner_id=str(company.get("ASSIGNED_BY_ID") or ""),
                    origin_id=str(company.get("ORIGIN_ID") or ""),
                )
            director = extract_director_from_text(str(company.get("COMMENTS") or ""))
            for key in alias_keys_for_value(director):
                add_record(
                    records,
                    alias_key=key,
                    source_type="company_comment_director",
                    source_id=company_id,
                    title=title,
                    raw_name=director,
                    company_id=company_id,
                    owner_id=str(company.get("ASSIGNED_BY_ID") or ""),
                    origin_id=str(company.get("ORIGIN_ID") or ""),
                )

    if include_deals:
        for deal in list_deals(client, only_eqazyna=only_eqazyna, include_closed=include_closed_deals, limit=max_deals or None):
            deal_id = str(deal.get("ID") or "")
            title = str(deal.get("TITLE") or "")
            director = extract_director_from_text(str(deal.get("COMMENTS") or ""))
            for key in alias_keys_for_value(director):
                add_record(
                    records,
                    alias_key=key,
                    source_type="deal_comment_director",
                    source_id=deal_id,
                    title=title,
                    raw_name=director,
                    contact_id=str(deal.get("CONTACT_ID") or ""),
                    company_id=str(deal.get("COMPANY_ID") or ""),
                    deal_id=deal_id,
                    owner_id=str(deal.get("ASSIGNED_BY_ID") or ""),
                    stage_id=str(deal.get("STAGE_ID") or ""),
                    closed=str(deal.get("CLOSED") or ""),
                    origin_id=str(deal.get("ORIGIN_ID") or ""),
                )
    return records


def write_csv(path: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_reports(records: list[AliasRecord], min_records_per_group: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_key: dict[str, list[AliasRecord]] = defaultdict(list)
    for r in records:
        by_key[r.alias_key].append(r)

    summary: list[dict[str, Any]] = []
    suspect_details: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []

    for key, group in sorted(by_key.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(group) < min_records_per_group:
            continue
        normalized_names = sorted({r.normalized_name for r in group if r.normalized_name})
        raw_names = sorted({r.raw_name for r in group if r.raw_name})
        contact_ids = sorted({r.contact_id for r in group if r.contact_id}, key=lambda x: int(x) if x.isdigit() else 10**12)
        company_ids = sorted({r.company_id for r in group if r.company_id}, key=lambda x: int(x) if x.isdigit() else 10**12)
        deal_ids = sorted({r.deal_id for r in group if r.deal_id}, key=lambda x: int(x) if x.isdigit() else 10**12)
        owner_ids = sorted({r.owner_id for r in group if r.owner_id}, key=lambda x: int(x) if x.isdigit() else 10**12)
        source_counts = Counter(r.source_type for r in group)

        is_suspect = len(raw_names) > 1 or len(contact_ids) > 1 or len(company_ids) > 1
        canonical = best_display_name(normalized_names or raw_names)
        row = {
            "alias_key": key,
            "canonical_guess": canonical,
            "is_suspect": "Y" if is_suspect else "N",
            "record_count": len(group),
            "distinct_raw_name_count": len(raw_names),
            "distinct_contact_count": len(contact_ids),
            "distinct_company_count": len(company_ids),
            "distinct_deal_count": len(deal_ids),
            "distinct_owner_count": len(owner_ids),
            "raw_names": " | ".join(raw_names),
            "contact_ids": ", ".join(contact_ids),
            "company_ids": ", ".join(company_ids),
            "deal_ids": ", ".join(deal_ids),
            "owner_ids": ", ".join(owner_ids),
            "source_types": " | ".join(f"{k}:{v}" for k, v in sorted(source_counts.items())),
        }
        summary.append(row)

        for r in group:
            detail = asdict(r)
            detail["is_suspect_group"] = row["is_suspect"]
            detail["canonical_guess"] = canonical
            all_details.append(detail)
            if is_suspect:
                suspect_details.append(detail)

    return summary, suspect_details, all_details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit possible duplicate director/contact names by alias key, e.g. ЛЯБАХ Г.Г. vs Геннадий Лябах.")
    parser.add_argument("--only-eqazyna", action="store_true", help="Limit deals/companies to ORIGINATOR_ID=EQAZYNA")
    parser.add_argument("--include-closed-deals", action="store_true")
    parser.add_argument("--include-contacts", action="store_true")
    parser.add_argument("--include-companies", action="store_true")
    parser.add_argument("--include-deals", action="store_true")
    parser.add_argument("--max-contacts", type=int, default=0)
    parser.add_argument("--max-companies", type=int, default=0)
    parser.add_argument("--max-deals", type=int, default=0)
    parser.add_argument("--min-records-per-group", type=int, default=2)
    parser.add_argument("--summary-out", default="exports/director_alias_groups.csv")
    parser.add_argument("--suspects-out", default="exports/director_alias_suspects.csv")
    parser.add_argument("--records-out", default="exports/director_alias_records.csv")
    args = parser.parse_args(argv)

    if not (args.include_contacts or args.include_companies or args.include_deals):
        args.include_contacts = True
        args.include_companies = True
        args.include_deals = True

    client = BitrixClient(
        webhook_url=os.environ.get("BITRIX_WEBHOOK_URL", ""),
        timeout=int(os.environ.get("REQUEST_TIMEOUT", "60")),
        polite_delay_seconds=float(os.environ.get("BITRIX_POLITE_DELAY_SECONDS", "0.15")),
    )

    records = collect_records(
        client,
        include_contacts=args.include_contacts,
        include_companies=args.include_companies,
        include_deals=args.include_deals,
        only_eqazyna=args.only_eqazyna,
        include_closed_deals=args.include_closed_deals,
        max_contacts=args.max_contacts,
        max_companies=args.max_companies,
        max_deals=args.max_deals,
    )
    summary, suspects, all_details = build_reports(records, args.min_records_per_group)

    summary_fields = [
        "alias_key",
        "canonical_guess",
        "is_suspect",
        "record_count",
        "distinct_raw_name_count",
        "distinct_contact_count",
        "distinct_company_count",
        "distinct_deal_count",
        "distinct_owner_count",
        "raw_names",
        "contact_ids",
        "company_ids",
        "deal_ids",
        "owner_ids",
        "source_types",
    ]
    detail_fields = [
        "is_suspect_group",
        "canonical_guess",
        "alias_key",
        "source_type",
        "source_id",
        "title",
        "raw_name",
        "normalized_name",
        "contact_id",
        "company_id",
        "deal_id",
        "owner_id",
        "stage_id",
        "closed",
        "origin_id",
        "url_hint",
    ]
    write_csv(args.summary_out, summary, summary_fields)
    write_csv(args.suspects_out, suspects, detail_fields)
    write_csv(args.records_out, all_details, detail_fields)

    print(
        f"DIRECTOR_ALIAS_AUDIT_OK records={len(records)} groups={len(summary)} suspect_records={len(suspects)} suspect_groups={sum(1 for r in summary if r['is_suspect'] == 'Y')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
