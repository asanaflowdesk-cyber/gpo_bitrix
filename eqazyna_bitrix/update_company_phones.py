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

import requests

from .bitrix_client import BitrixClient, BitrixError

MARKER_PREFIX = "AUTO_PHONE_IMPORT_BIN:"
DIRECTOR_MARKER_PREFIX = "AUTO_DIRECTOR_CONTACT_PHONE_IMPORT_BIN:"
DEFAULT_COMMENT_SOURCE = "файл BIN/MOBILE"

MAX_RETRIES = 6
RETRY_SLEEP_SECONDS = 3

DIRECTOR_POST_KEYWORDS = (
    "директор",
    "руковод",
    "генераль",
    "исполнительн",
    "председатель",
    "owner",
    "ceo",
    "director",
    "head",
)


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
    contact_ids: str
    contact_names: str
    contact_comment_action: str
    action: str
    error: str | None = None


class DirectBitrixRest:
    """Минимальный REST-клиент для методов, которых может не быть в bitrix_client.py."""

    def __init__(self, webhook_url: str, timeout: int = 60):
        if not webhook_url:
            raise ValueError("BITRIX_WEBHOOK_URL is empty")
        self.webhook_url = webhook_url.rstrip("/") + "/"
        self.timeout = timeout

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        payload = payload or {}
        url = self.webhook_url + method + ".json"
        last_error: Any = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                try:
                    data = response.json()
                except Exception as exc:  # noqa: BLE001
                    raise BitrixError(
                        f"Bitrix returned non-JSON response in {method}. "
                        f"HTTP {response.status_code}. Text: {response.text[:500]}"
                    ) from exc

                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = data
                    time.sleep(RETRY_SLEEP_SECONDS * attempt)
                    continue

                if isinstance(data, dict) and data.get("error"):
                    error = str(data.get("error"))
                    description = str(data.get("error_description", ""))
                    if error in {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "OVERLOAD_LIMIT"}:
                        last_error = data
                        time.sleep(RETRY_SLEEP_SECONDS * attempt)
                        continue
                    raise BitrixError(f"Bitrix API error in {method}: {error} — {description}")

                return data.get("result") if isinstance(data, dict) else data

            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(RETRY_SLEEP_SECONDS * attempt)

        raise BitrixError(f"Bitrix API failed after {MAX_RETRIES} attempts. Method: {method}. Last error: {last_error}")


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
                rows.append([cells.get(i, "") for i in range(max_col + 1)])

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
            grouped[bin_number] = PhoneImportRow(
                bin=bin_number,
                raw_bin=str(raw_bin),
                raw_mobile=str(raw_mobile),
                normalized_phones=[],
            )
        else:
            grouped[bin_number].raw_mobile = ", ".join(
                x for x in [grouped[bin_number].raw_mobile, str(raw_mobile)] if x
            )
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
        f"Добавлены в карточку компании: {', '.join(phones_to_add) or 'нет новых номеров'}\n"
        f"{marker}"
    )
    return (old_comment or "").rstrip() + block


def contact_display_name(contact: dict[str, Any]) -> str:
    parts = [
        str(contact.get("LAST_NAME") or "").strip(),
        str(contact.get("NAME") or "").strip(),
        str(contact.get("SECOND_NAME") or "").strip(),
    ]
    fio = " ".join(x for x in parts if x).strip()
    return fio or str(contact.get("ID") or "").strip()


def get_contact_ids_from_company_payload(company: dict[str, Any]) -> list[str]:
    """Фолбэк: если find_company_by_requisite_bin уже вернул CONTACT_ID/CONTACT_IDS."""
    result: list[str] = []
    for key in ("CONTACT_ID", "CONTACT_IDS", "CONTACTS"):
        value = company.get(key)
        if not value:
            continue
        if isinstance(value, (str, int)):
            result.append(str(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int)):
                    result.append(str(item))
                elif isinstance(item, dict):
                    contact_id = item.get("CONTACT_ID") or item.get("ID") or item.get("contactId")
                    if contact_id:
                        result.append(str(contact_id))
    return unique_values(result)


def unique_values(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def get_company_contact_ids(rest: DirectBitrixRest, company: dict[str, Any], company_id: str) -> list[str]:
    contact_ids = get_contact_ids_from_company_payload(company)

    try:
        linked = rest.call("crm.company.contact.items.get", {"id": int(company_id)})
    except BitrixError:
        linked = []

    if isinstance(linked, list):
        for item in linked:
            if isinstance(item, dict):
                contact_id = item.get("CONTACT_ID") or item.get("ID") or item.get("contactId")
                if contact_id:
                    contact_ids.append(str(contact_id))
            elif isinstance(item, (str, int)):
                contact_ids.append(str(item))

    return unique_values(contact_ids)


def is_director_like_contact(contact: dict[str, Any]) -> bool:
    text = " ".join(
        str(contact.get(key) or "")
        for key in ("POST", "TYPE_ID", "COMMENTS")
    ).lower()
    return any(keyword in text for keyword in DIRECTOR_POST_KEYWORDS)


def select_contacts_for_comment(
    contacts: list[dict[str, Any]],
    *,
    strict_director_match: bool,
) -> tuple[list[dict[str, Any]], str]:
    if not contacts:
        return [], "no_linked_contact"

    if len(contacts) == 1:
        return contacts, "single_linked_contact"

    director_like = [contact for contact in contacts if is_director_like_contact(contact)]
    if director_like:
        return director_like, "director_like_contact"

    if strict_director_match:
        return [], "multiple_contacts_unclear"

    # Практичный режим по умолчанию: если контактов несколько и должность не заполнена,
    # пишем во все привязанные контакты. Иначе Excel опять "доехал, но не туда".
    return contacts, "all_linked_contacts_no_director_flag"


def director_marker_begin(row: PhoneImportRow) -> str:
    return f"[{DIRECTOR_MARKER_PREFIX}{row.bin}_BEGIN]"


def director_marker_end(row: PhoneImportRow) -> str:
    return f"[{DIRECTOR_MARKER_PREFIX}{row.bin}_END]"


def build_director_contact_block(
    row: PhoneImportRow,
    *,
    company_id: str,
    company_title: str,
    source_label: str,
) -> str:
    date_str = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{director_marker_begin(row)}\n"
        "[Дополнительные контакты из Excel]\n"
        f"Источник: {source_label}\n"
        f"Дата: {date_str}\n"
        f"Компания: {company_title or '-'}\n"
        f"ID компании: {company_id or '-'}\n"
        f"БИН: {row.bin}\n"
        f"Исходное значение MOBILE: {row.raw_mobile or '-'}\n"
        f"Нормализованные телефоны: {', '.join(row.normalized_phones) or '-'}\n"
        f"{director_marker_end(row)}"
    )


def upsert_director_comment(old_comment: str, row: PhoneImportRow, new_block: str) -> tuple[str, bool]:
    old_comment = old_comment or ""
    begin = re.escape(director_marker_begin(row))
    end = re.escape(director_marker_end(row))
    pattern = re.compile(begin + r".*?" + end, flags=re.DOTALL)
    marker_present = bool(pattern.search(old_comment))

    if marker_present:
        return pattern.sub(new_block, old_comment).strip(), True

    if old_comment.strip():
        return old_comment.rstrip() + "\n\n" + new_block, False

    return new_block, False


def update_director_contact_comments(
    rest: DirectBitrixRest,
    *,
    company: dict[str, Any],
    company_id: str,
    company_title: str,
    row: PhoneImportRow,
    source_label: str,
    dry_run: bool,
    force: bool,
    strict_director_match: bool,
) -> tuple[str, str, str, str | None]:
    """
    Пишет доп. контакты из Excel в COMMENTS контакта-руководителя.

    Логика:
    1. Берём привязанные к компании контакты.
    2. Если контакт один — обновляем его.
    3. Если контактов несколько — сначала ищем по должности директор/руководитель.
    4. Если должность не заполнена и strict_director_match=False — обновляем все привязанные контакты.
    """
    try:
        contact_ids = get_company_contact_ids(rest, company, company_id)
        if not contact_ids:
            return "", "", "no_linked_contact", None

        contacts: list[dict[str, Any]] = []
        for contact_id in contact_ids:
            contact = rest.call("crm.contact.get", {"id": int(contact_id)})
            if isinstance(contact, dict) and contact:
                contacts.append(contact)

        selected_contacts, select_action = select_contacts_for_comment(
            contacts,
            strict_director_match=strict_director_match,
        )
        if not selected_contacts:
            names = "; ".join(contact_display_name(c) for c in contacts)
            return ", ".join(contact_ids), names, select_action, None

        updated_ids: list[str] = []
        updated_names: list[str] = []
        skipped_ids: list[str] = []

        for contact in selected_contacts:
            contact_id = str(contact.get("ID") or "").strip()
            if not contact_id:
                continue

            old_comment = str(contact.get("COMMENTS") or "")
            new_block = build_director_contact_block(
                row,
                company_id=company_id,
                company_title=company_title,
                source_label=source_label,
            )
            new_comment, marker_present = upsert_director_comment(old_comment, row, new_block)

            if marker_present and not force:
                skipped_ids.append(contact_id)
                continue

            if not dry_run:
                rest.call(
                    "crm.contact.update",
                    {
                        "id": int(contact_id),
                        "fields": {
                            "COMMENTS": new_comment,
                        },
                    },
                )

            updated_ids.append(contact_id)
            updated_names.append(contact_display_name(contact))

        if updated_ids:
            action_prefix = "dry_run_contact_comment" if dry_run else "contact_comment_updated"
            return ", ".join(updated_ids), "; ".join(updated_names), f"{action_prefix}:{select_action}", None

        if skipped_ids:
            names = "; ".join(contact_display_name(c) for c in selected_contacts)
            return ", ".join(skipped_ids), names, "contact_comment_skipped_already_processed", None

        return ", ".join(contact_ids), "", "contact_comment_no_target", None

    except BitrixError as exc:
        return "", "", "contact_comment_error", str(exc)


def process_rows(
    client: BitrixClient,
    rest: DirectBitrixRest,
    rows: list[PhoneImportRow],
    *,
    bin_field: str,
    dry_run: bool,
    force: bool,
    source_label: str,
    skip_director_comments: bool,
    strict_director_match: bool,
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
                    contact_ids="",
                    contact_names="",
                    contact_comment_action="not_processed",
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
                    contact_ids="",
                    contact_names="",
                    contact_comment_action="not_processed",
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
                        contact_ids="",
                        contact_names="",
                        contact_comment_action="not_processed",
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

            company_action = "skipped_already_processed"
            company_error: str | None = None

            if marker_present and not force:
                company_action = "skipped_already_processed"
            else:
                fields: dict[str, Any] = {
                    "COMMENTS": build_comment(comments, row, phones_to_add, source_label),
                }
                if phones_to_add:
                    fields["PHONE"] = build_phone_payload(company, phones_to_add)

                company_action = "dry_run_update" if dry_run else "updated"
                if not dry_run:
                    client.update_company(company_id, fields)

            contact_ids = ""
            contact_names = ""
            contact_comment_action = "skipped_by_arg"
            contact_error: str | None = None

            if not skip_director_comments:
                contact_ids, contact_names, contact_comment_action, contact_error = update_director_contact_comments(
                    rest,
                    company=company,
                    company_id=company_id,
                    company_title=title,
                    row=row,
                    source_label=source_label,
                    dry_run=dry_run,
                    force=force,
                    strict_director_match=strict_director_match,
                )

            error_parts = [x for x in [company_error, contact_error] if x]
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
                    contact_ids=contact_ids,
                    contact_names=contact_names,
                    contact_comment_action=contact_comment_action,
                    action=company_action,
                    error=" | ".join(error_parts) if error_parts else None,
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
                    contact_ids="",
                    contact_names="",
                    contact_comment_action="not_processed",
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
    parser.add_argument("--force", action="store_true", help="Update even if import markers already exist")
    parser.add_argument("--bin-field", default=os.getenv("BITRIX_REQUISITE_BIN_FIELD", "RQ_BIN"))
    parser.add_argument("--source-label", default=DEFAULT_COMMENT_SOURCE)
    parser.add_argument("--skip-director-comments", action="store_true", help="Do not write Excel contacts to linked director/contact COMMENTS")
    parser.add_argument(
        "--strict-director-match",
        action="store_true",
        help="If company has multiple contacts, update only contacts whose POST looks like director/head. Default: update all linked contacts when director is unclear.",
    )
    parser.add_argument("--out", default="exports/update_company_phones_log.json")
    parser.add_argument("--csv-out", default="exports/update_company_phones_log.csv")
    args = parser.parse_args(argv)

    input_path = Path(args.file)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    webhook_url = os.getenv("BITRIX_WEBHOOK_URL", "")
    if not webhook_url:
        raise RuntimeError("BITRIX_WEBHOOK_URL is empty")

    timeout = int(os.getenv("REQUEST_TIMEOUT", "60"))
    client = BitrixClient(webhook_url=webhook_url, timeout=timeout)
    rest = DirectBitrixRest(webhook_url=webhook_url, timeout=timeout)

    rows = load_phone_rows(input_path)
    results = process_rows(
        client,
        rest,
        rows,
        bin_field=args.bin_field,
        dry_run=args.dry_run,
        force=args.force,
        source_label=args.source_label,
        skip_director_comments=args.skip_director_comments,
        strict_director_match=args.strict_director_match,
    )
    write_json(Path(args.out), results)
    write_csv(Path(args.csv_out), results)

    counts: dict[str, int] = {}
    contact_counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1
        contact_counts[result.contact_comment_action] = contact_counts.get(result.contact_comment_action, 0) + 1

    print("PHONE_IMPORT_SUMMARY")
    print(
        json.dumps(
            {
                "total_bins": len(results),
                "dry_run": args.dry_run,
                "force": args.force,
                "counts": counts,
                "contact_comment_counts": contact_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
