from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .bitrix_client import BitrixClient, BitrixError

MARKER_PREFIX = "AUTO_PHONE_IMPORT_BIN:"
DEFAULT_COMMENT_SOURCE = "файл BIN/MOBILE"


@dataclass(slots=True)
class PhoneImportRow:
    bin: str
    raw_bin: str
    raw_mobile: str
    normalized_phones: list[str]


@dataclass(slots=True)
class PhoneUpdateResult:
    bin: str
    raw_bin: str
    company_id: str | None
    company_title: str | None
    input_mobile: str
    normalized_phones: str
    existing_phones: str
    new_phones: str
    marker_present: bool
    action: str
    error: str | None = None


def normalize_bin(value: Any) -> str:
    raw = "" if value is None else str(value).strip()
    # Excel sometimes renders numeric values as 60740008536.0
    if raw.endswith(".0") and raw.replace(".0", "", 1).isdigit():
        raw = raw[:-2]
    digits = re.sub(r"\D+", "", raw)
    if digits and len(digits) < 12:
        digits = digits.zfill(12)
    return digits


def normalize_phone(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    elif len(digits) > 11 and digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 10 or len(digits) > 15:
        return None
    return "+" + digits


def split_phones(value: Any) -> list[str]:
    raw = "" if value is None else str(value)
    parts = re.split(r"[,;\n\r]+", raw)
    phones: list[str] = []
    seen: set[str] = set()
    for part in parts:
        phone = normalize_phone(part)
        if phone and phone not in seen:
            seen.add(phone)
            phones.append(phone)
    return phones


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _read_xlsx_first_sheet(path: Path) -> list[dict[str, str]]:
    """Read a simple .xlsx using only stdlib. Expects headers in the first row."""
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rel = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns_main}si"):
                texts = [t.text or "" for t in si.iter(f"{ns_main}t")]
                shared.append("".join(texts))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        first_sheet = workbook.find(f"{ns_main}sheets/{ns_main}sheet")
        if first_sheet is None:
            return []
        rel_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels.findall(f"{ns_rel}Relationship"):
            if rel.attrib.get("Id") == rel_id:
                target = rel.attrib.get("Target")
                break
        if not target:
            return []
        sheet_path = "xl/" + target.lstrip("/")
        sheet_path = sheet_path.replace("xl/xl/", "xl/")
        sheet = ET.fromstring(zf.read(sheet_path))

        rows: list[list[str]] = []
        for row in sheet.iter(f"{ns_main}row"):
            values: list[str] = []
            max_col = -1
            cells: dict[int, str] = {}
            for c in row.findall(f"{ns_main}c"):
                ref = c.attrib.get("r", "A1")
                col_idx = _column_index(ref)
                max_col = max(max_col, col_idx)
                cell_type = c.attrib.get("t")
                value_node = c.find(f"{ns_main}v")
                inline_node = c.find(f"{ns_main}is")
                value = ""
                if cell_type == "s" and value_node is not None:
                    try:
                        value = shared[int(value_node.text or "0")]
                    except Exception:  # noqa: BLE001
                        value = value_node.text or ""
                elif cell_type == "inlineStr" and inline_node is not None:
                    value = "".join(t.text or "" for t in inline_node.iter(f"{ns_main}t"))
                elif value_node is not None:
                    value = value_node.text or ""
                cells[col_idx] = value
            if max_col >= 0:
                values = [cells.get(i, "") for i in range(max_col + 1)]
                rows.append(values)

    if not rows:
        return []
    headers = [str(h).strip() for h in rows[0]]
    result: list[dict[str, str]] = []
    for row in rows[1:]:
        if not any(str(v).strip() for v in row):
            continue
        item = {headers[i]: str(row[i]).strip() if i < len(row) else "" for i in range(len(headers))}
        result.append(item)
    return result


def read_table(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _read_xlsx_first_sheet(path)
    if suffix in {".csv", ".txt"}:
        text = path.read_text(encoding="utf-8-sig")
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(text.splitlines(), dialect=dialect))
    raise ValueError(f"Unsupported file type: {suffix}. Use .xlsx or .csv")


def find_column(headers: list[str], candidates: list[str]) -> str | None:
    normalized = {re.sub(r"\s+", "", h).lower(): h for h in headers}
    for candidate in candidates:
        key = re.sub(r"\s+", "", candidate).lower()
        if key in normalized:
            return normalized[key]
    for h in headers:
        h_norm = re.sub(r"\s+", "", h).lower()
        if any(re.sub(r"\s+", "", c).lower() in h_norm for c in candidates):
            return h
    return None


def load_phone_rows(path: Path) -> list[PhoneImportRow]:
    raw_rows = read_table(path)
    if not raw_rows:
        return []
    headers = list(raw_rows[0].keys())
    bin_col = find_column(headers, ["BIN", "БИН", "ИИН/БИН", "IINBIN", "БИН/ИИН"])
    phone_col = find_column(headers, ["MOBILE", "PHONE", "Телефон", "Телефоны", "Мобильный", "Номер"])
    if not bin_col or not phone_col:
        raise ValueError(f"Need BIN and MOBILE columns. Found headers: {headers}")

    grouped: dict[str, PhoneImportRow] = {}
    for raw in raw_rows:
        raw_bin = raw.get(bin_col, "")
        bin_number = normalize_bin(raw_bin)
        raw_mobile = raw.get(phone_col, "")
        phones = split_phones(raw_mobile)
        if not bin_number:
            continue
        if bin_number not in grouped:
            grouped[bin_number] = PhoneImportRow(bin=bin_number, raw_bin=str(raw_bin), raw_mobile=str(raw_mobile), normalized_phones=[])
        else:
            grouped[bin_number].raw_mobile = ", ".join(x for x in [grouped[bin_number].raw_mobile, str(raw_mobile)] if x)
        seen = set(grouped[bin_number].normalized_phones)
        for phone in phones:
            if phone not in seen:
                seen.add(phone)
                grouped[bin_number].normalized_phones.append(phone)
    return list(grouped.values())


def phone_values(company: dict[str, Any]) -> list[dict[str, Any]]:
    values = company.get("PHONE") or []
    return values if isinstance(values, list) else []


def normalized_existing_phones(company: dict[str, Any]) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for item in phone_values(company):
        if not isinstance(item, dict):
            continue
        phone = normalize_phone(str(item.get("VALUE") or ""))
        if phone and phone not in seen:
            seen.add(phone)
            phones.append(phone)
    return phones


def build_phone_payload(company: dict[str, Any], phones_to_add: list[str]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in phone_values(company):
        if not isinstance(item, dict):
            continue
        existing: dict[str, Any] = {}
        if item.get("ID"):
            existing["ID"] = item.get("ID")
        existing["VALUE"] = item.get("VALUE") or ""
        existing["VALUE_TYPE"] = item.get("VALUE_TYPE") or "WORK"
        if existing["VALUE"]:
            payload.append(existing)
    for phone in phones_to_add:
        payload.append({"VALUE": phone, "VALUE_TYPE": "WORK"})
    return payload


def build_comment(old_comment: str, row: PhoneImportRow, phones_to_add: list[str], source_label: str) -> str:
    date_str = time.strftime("%Y-%m-%d %H:%M:%S")
    marker = f"{MARKER_PREFIX}{row.bin}"
    block = (
        "\n\n[Автообновление телефонов]\n"
        f"Источник: {source_label}\n"
        f"Дата: {date_str}\n"
        f"БИН: {row.bin}\n"
        f"Телефоны из файла: {', '.join(row.normalized_phones) or '-'}\n"
        f"Добавлены в карточку: {', '.join(phones_to_add) or 'нет новых номеров'}\n"
        f"{marker}"
    )
    return (old_comment or "").rstrip() + block


def process_rows(
    client: BitrixClient,
    rows: list[PhoneImportRow],
    *,
    bin_field: str,
    dry_run: bool,
    force: bool,
    source_label: str,
) -> list[PhoneUpdateResult]:
    results: list[PhoneUpdateResult] = []
    for row in rows:
        if len(row.bin) != 12:
            results.append(
                PhoneUpdateResult(
                    bin=row.bin,
                    raw_bin=row.raw_bin,
                    company_id=None,
                    company_title=None,
                    input_mobile=row.raw_mobile,
                    normalized_phones=", ".join(row.normalized_phones),
                    existing_phones="",
                    new_phones="",
                    marker_present=False,
                    action="invalid_bin",
                    error="BIN must contain 12 digits after normalization",
                )
            )
            continue
        if not row.normalized_phones:
            results.append(
                PhoneUpdateResult(
                    bin=row.bin,
                    raw_bin=row.raw_bin,
                    company_id=None,
                    company_title=None,
                    input_mobile=row.raw_mobile,
                    normalized_phones="",
                    existing_phones="",
                    new_phones="",
                    marker_present=False,
                    action="no_valid_phone",
                    error="No valid phones found in MOBILE column",
                )
            )
            continue
        try:
            company = client.find_company_by_requisite_bin(row.bin, bin_field=bin_field)
            if not company:
                results.append(
                    PhoneUpdateResult(
                        bin=row.bin,
                        raw_bin=row.raw_bin,
                        company_id=None,
                        company_title=None,
                        input_mobile=row.raw_mobile,
                        normalized_phones=", ".join(row.normalized_phones),
                        existing_phones="",
                        new_phones="",
                        marker_present=False,
                        action="company_not_found",
                    )
                )
                continue

            company_id = str(company.get("ID") or "")
            title = str(company.get("TITLE") or "")
            comments = str(company.get("COMMENTS") or "")
            marker = f"{MARKER_PREFIX}{row.bin}"
            marker_present = marker in comments
            existing = normalized_existing_phones(company)
            existing_set = set(existing)
            phones_to_add = [p for p in row.normalized_phones if p not in existing_set]

            if marker_present and not force:
                results.append(
                    PhoneUpdateResult(
                        bin=row.bin,
                        raw_bin=row.raw_bin,
                        company_id=company_id,
                        company_title=title,
                        input_mobile=row.raw_mobile,
                        normalized_phones=", ".join(row.normalized_phones),
                        existing_phones=", ".join(existing),
                        new_phones=", ".join(phones_to_add),
                        marker_present=True,
                        action="skipped_already_processed",
                    )
                )
                continue

            fields: dict[str, Any] = {
                "COMMENTS": build_comment(comments, row, phones_to_add, source_label),
            }
            if phones_to_add:
                fields["PHONE"] = build_phone_payload(company, phones_to_add)

            action = "dry_run_update" if dry_run else "updated"
            if not dry_run:
                client.update_company(company_id, fields)

            results.append(
                PhoneUpdateResult(
                    bin=row.bin,
                    raw_bin=row.raw_bin,
                    company_id=company_id,
                    company_title=title,
                    input_mobile=row.raw_mobile,
                    normalized_phones=", ".join(row.normalized_phones),
                    existing_phones=", ".join(existing),
                    new_phones=", ".join(phones_to_add),
                    marker_present=marker_present,
                    action=action,
                )
            )
        except BitrixError as exc:
            results.append(
                PhoneUpdateResult(
                    bin=row.bin,
                    raw_bin=row.raw_bin,
                    company_id=None,
                    company_title=None,
                    input_mobile=row.raw_mobile,
                    normalized_phones=", ".join(row.normalized_phones),
                    existing_phones="",
                    new_phones="",
                    marker_present=False,
                    action="error",
                    error=str(exc),
                )
            )
    return results


def write_json(path: Path, results: list[PhoneUpdateResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, results: list[PhoneUpdateResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(PhoneUpdateResult.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update Bitrix company phones by BIN from XLSX/CSV file")
    parser.add_argument("--file", required=True, help="Path to .xlsx/.csv file with BIN and MOBILE columns")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Bitrix")
    parser.add_argument("--force", action="store_true", help="Update even if AUTO_PHONE_IMPORT_BIN marker already exists")
    parser.add_argument("--bin-field", default=os.getenv("BITRIX_REQUISITE_BIN_FIELD", "RQ_BIN"))
    parser.add_argument("--source-label", default=DEFAULT_COMMENT_SOURCE)
    parser.add_argument("--out", default="exports/update_company_phones_log.json")
    parser.add_argument("--csv-out", default="exports/update_company_phones_log.csv")
    args = parser.parse_args(argv)

    input_path = Path(args.file)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    webhook_url = os.getenv("BITRIX_WEBHOOK_URL", "")
    timeout = int(os.getenv("REQUEST_TIMEOUT", "60"))
    client = BitrixClient(webhook_url=webhook_url, timeout=timeout)

    rows = load_phone_rows(input_path)
    results = process_rows(
        client,
        rows,
        bin_field=args.bin_field,
        dry_run=args.dry_run,
        force=args.force,
        source_label=args.source_label,
    )
    write_json(Path(args.out), results)
    write_csv(Path(args.csv_out), results)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
    print("PHONE_IMPORT_SUMMARY")
    print(json.dumps({"total_bins": len(results), "dry_run": args.dry_run, "force": args.force, "counts": counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
