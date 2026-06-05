from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "eqazyna_bitrix"
CONFIG = PKG / "config"
WORKFLOWS = ROOT / ".github" / "workflows"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fail(message: str) -> None:
    print(f"VALIDATION_ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_yaml(path: Path) -> Any:
    if not path.exists():
        fail(f"missing file: {path.relative_to(ROOT)}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        fail(f"invalid yaml {path.relative_to(ROOT)}: {exc}")


def check_workflows() -> None:
    if not WORKFLOWS.exists():
        fail("missing .github/workflows directory")

    forbidden = [
        WORKFLOWS / "managers.yml",
    ]
    for path in forbidden:
        if path.exists():
            fail(f"config file is in workflows directory: {path.relative_to(ROOT)}")

    workflow_files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not workflow_files:
        fail("no workflow yaml files found")

    for path in workflow_files:
        data = read_yaml(path)
        if not isinstance(data, dict):
            fail(f"workflow root is not mapping: {path.relative_to(ROOT)}")
        # PyYAML 1.1 parses key 'on' as boolean True. Accept both.
        has_on = "on" in data or True in data
        if "name" not in data or not has_on or "jobs" not in data:
            fail(f"workflow misses name/on/jobs: {path.relative_to(ROOT)}")


def check_configs() -> None:
    read_yaml(CONFIG / "managers.yml")

    from eqazyna_bitrix.config.managers import load_manager_config

    managers = load_manager_config()
    known = set(managers.user_names)

    for user_id in managers.source_responsible_ids:
        if user_id not in known:
            fail(f"source_responsible_ids references unknown technical user id: {user_id}")

    summary = {
        "active_manager_count": len(managers.allowed_user_ids),
        "active_manager_ids": managers.allowed_user_ids,
        "technical_source_responsible_ids": managers.source_responsible_ids,
    }
    print("CONFIG_VALIDATION_SUMMARY")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def check_imports() -> None:
    import eqazyna_bitrix.main  # noqa: F401
    import eqazyna_bitrix.pipeline  # noqa: F401
    import eqazyna_bitrix.distribute_companies  # noqa: F401
    import eqazyna_bitrix.audit_repair_deal_packages  # noqa: F401


def main() -> int:
    check_workflows()
    check_configs()
    check_imports()
    print("REPOSITORY_VALIDATION_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
