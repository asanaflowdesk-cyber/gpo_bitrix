from __future__ import annotations

import json
import os
from pathlib import Path

from .bitrix_client import BitrixClient


def main() -> int:
    webhook = os.getenv("BITRIX_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("BITRIX_WEBHOOK_URL is empty")
    out_dir = Path("exports/diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = BitrixClient(webhook)
    methods = {
        "company_fields": "crm.company.fields",
        "deal_fields": "crm.deal.fields",
        "requisite_fields": "crm.requisite.fields",
        "requisite_presets": "crm.requisite.preset.list",
        "statuses": "crm.status.list",
    }
    for name, method in methods.items():
        print(f"Calling {method}")
        payload = {"select": ["ID", "NAME", "COUNTRY_ID", "SORT", "ACTIVE"]} if method == "crm.requisite.preset.list" else {}
        result = client.call(method, payload)
        (out_dir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Diagnostics saved to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
