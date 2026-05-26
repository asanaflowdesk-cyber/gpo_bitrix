from __future__ import annotations

import json
import os
from pathlib import Path

from .egov_client import EgovClient
from .settings import Settings


def main() -> int:
    settings = Settings.from_env()
    bin_number = os.getenv("BIN") or os.getenv("TEST_BIN") or ""
    if not bin_number:
        raise SystemExit("BIN env variable is required")

    applicant_name = os.getenv("COMPANY_NAME") or os.getenv("APPLICANT_NAME") or ""
    result = EgovClient(settings.egov_api_key, timeout=settings.request_timeout).get_company(bin_number, applicant_name)
    out_dir = Path("exports/egov-diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    data = result.as_dict()
    (out_dir / f"egov_{bin_number}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
