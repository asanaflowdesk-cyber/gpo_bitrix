#!/usr/bin/env python3
"""Force one Bitrix CRM entity owner and verify the actual ASSIGNED_BY_ID.

Diagnostic admin tool. It is intentionally narrow: one entity by ID.
Use it to separate script matching issues from Bitrix permission/robot issues.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

TRUE_VALUES = {"1", "true", "yes", "y", "да", "д", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "н", "off", ""}
METHODS = {
    "deal": {"get": "crm.deal.get", "update": "crm.deal.update"},
    "company": {"get": "crm.company.get", "update": "crm.company.update"},
    "contact": {"get": "crm.contact.get", "update": "crm.contact.update"},
}


class BitrixError(RuntimeError):
    pass


def s(value: Any) -> str:
    return str(value or "").strip()


def norm_id(value: Any) -> str:
    raw = s(value)
    if not raw:
        return ""
    try:
        return str(int(raw))
    except Exception:
        return raw


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    text = s(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def normalize_webhook(url: str) -> str:
    url = s(url)
    if not url:
        raise BitrixError("BITRIX_WEBHOOK_URL is empty")
    return url.rstrip("/") + "/"


class Bitrix:
    def __init__(self, webhook_url: str, timeout: int = 60) -> None:
        self.base = normalize_webhook(webhook_url)
        self.timeout = timeout
        self.session = requests.Session()

    def call_json_full(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(self.base + method + ".json", json=payload, timeout=self.timeout)
        return self._parse_response(method, response)

    def call_json(self, method: str, payload: Dict[str, Any]) -> Any:
        return self.call_json_full(method, payload).get("result")

    def call_form_full(self, method: str, params: List[Tuple[str, Any]]) -> Dict[str, Any]:
        response = self.session.post(self.base + method + ".json", data=params, timeout=self.timeout)
        return self._parse_response(method, response)

    def _parse_response(self, method: str, response: requests.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except Exception as exc:
            raise BitrixError(f"{method}: HTTP {response.status_code}: {response.text[:800]}") from exc
        if response.status_code >= 400 or "error" in data:
            raise BitrixError(f"{method}: {json.dumps(data, ensure_ascii=False)[:2000]}")
        return data

    def get_entity(self, entity_type: str, entity_id: str) -> Dict[str, Any]:
        method = METHODS[entity_type]["get"]
        result = self.call_json(method, {"id": entity_id})
        return dict(result or {})

    def get_owner(self, entity_type: str, entity_id: str) -> str:
        return norm_id(self.get_entity(entity_type, entity_id).get("ASSIGNED_BY_ID"))

    def validate_user(self, user_id: str) -> str:
        result = self.call_json("user.get", {"ID": user_id})
        user = dict(result[0]) if isinstance(result, list) and result else {}
        if not user:
            raise ValueError(f"target_user_id={user_id} not found by user.get")
        active = s(user.get("ACTIVE")).lower()
        if active in {"false", "n", "0", "нет"}:
            raise ValueError(f"target_user_id={user_id} is inactive")
        return " ".join(s(x) for x in [user.get("LAST_NAME"), user.get("NAME"), user.get("SECOND_NAME")] if s(x)).strip() or s(user.get("EMAIL")) or f"ID {user_id}"

    def update_owner_form(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        method = METHODS[entity_type]["update"]
        return self.call_form_full(method, [
            ("id", entity_id),
            ("fields[ASSIGNED_BY_ID]", target_user_id),
            ("params[REGISTER_SONET_EVENT]", "N"),
        ])

    def update_owner_json(self, entity_type: str, entity_id: str, target_user_id: str) -> Dict[str, Any]:
        method = METHODS[entity_type]["update"]
        return self.call_json_full(method, {
            "id": entity_id,
            "fields": {"ASSIGNED_BY_ID": target_user_id},
            "params": {"REGISTER_SONET_EVENT": "N"},
        })


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "entity_type", "entity_id", "target_user_id", "target_user_name", "dry_run",
        "before_owner_id", "after_form_owner_id", "after_json_owner_id", "after_delay_owner_id",
        "form_result", "json_result", "final_status", "error",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description="Force one Bitrix CRM entity ASSIGNED_BY_ID and verify it")
    parser.add_argument("--entity-type", required=True, choices=sorted(METHODS.keys()))
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--target-user-id", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--verify-delay-seconds", default="5")
    parser.add_argument("--out", default="exports/force_crm_owner_log.csv")
    parser.add_argument("--json-out", default="exports/force_crm_owner_summary.json")
    args = parser.parse_args()

    entity_type = args.entity_type
    entity_id = norm_id(args.entity_id)
    target_user_id = norm_id(args.target_user_id)
    dry_run = parse_bool(args.dry_run, default=True)
    verify_delay = max(0, int(s(args.verify_delay_seconds) or "5"))

    bx = Bitrix(os.getenv("BITRIX_WEBHOOK_URL", ""), timeout=int(os.getenv("REQUEST_TIMEOUT", "60")))
    target_user_name = bx.validate_user(target_user_id)

    row: Dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "target_user_id": target_user_id,
        "target_user_name": target_user_name,
        "dry_run": str(dry_run).lower(),
        "before_owner_id": "",
        "after_form_owner_id": "",
        "after_json_owner_id": "",
        "after_delay_owner_id": "",
        "form_result": "",
        "json_result": "",
        "final_status": "",
        "error": "",
    }

    try:
        before = bx.get_owner(entity_type, entity_id)
        row["before_owner_id"] = before
        print(f"MODE: {'DRY_RUN' if dry_run else 'WRITE'}")
        print(f"ENTITY: {entity_type} {entity_id}")
        print(f"BEFORE ASSIGNED_BY_ID: {before}")
        print(f"TARGET ASSIGNED_BY_ID: {target_user_id} ({target_user_name})")

        if dry_run:
            row["final_status"] = "dry_run_no_write"
        else:
            form_payload = bx.update_owner_form(entity_type, entity_id, target_user_id)
            row["form_result"] = json.dumps(form_payload.get("result"), ensure_ascii=False)[:500]
            time.sleep(1)
            after_form = bx.get_owner(entity_type, entity_id)
            row["after_form_owner_id"] = after_form
            print(f"AFTER FORM UPDATE ASSIGNED_BY_ID: {after_form}")

            if after_form != target_user_id:
                json_payload = bx.update_owner_json(entity_type, entity_id, target_user_id)
                row["json_result"] = json.dumps(json_payload.get("result"), ensure_ascii=False)[:500]
                time.sleep(1)
                after_json = bx.get_owner(entity_type, entity_id)
                row["after_json_owner_id"] = after_json
                print(f"AFTER JSON UPDATE ASSIGNED_BY_ID: {after_json}")
            else:
                row["after_json_owner_id"] = after_form

            if verify_delay:
                time.sleep(verify_delay)
            after_delay = bx.get_owner(entity_type, entity_id)
            row["after_delay_owner_id"] = after_delay
            print(f"AFTER {verify_delay}s ASSIGNED_BY_ID: {after_delay}")

            if after_delay == target_user_id:
                row["final_status"] = "owner_changed_and_verified"
            elif row["after_form_owner_id"] == target_user_id or row["after_json_owner_id"] == target_user_id:
                row["final_status"] = "changed_then_rolled_back"
            else:
                row["final_status"] = "update_accepted_but_owner_not_changed"
                raise BitrixError(
                    f"Owner did not change. before={before}, after_form={row['after_form_owner_id']}, "
                    f"after_json={row['after_json_owner_id']}, after_delay={after_delay}, target={target_user_id}"
                )
    except Exception as exc:
        row["error"] = str(exc)
        write_csv(args.out, [row])
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 1

    write_csv(args.out, [row])
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
