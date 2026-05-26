from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import ConfigError, as_int, as_str, read_yaml, resolve_config_path


@dataclass(frozen=True)
class ManagerConfig:
    allowed_user_ids: list[int]
    user_names: dict[int, str]
    source_responsible_ids: list[int]
    branch_by_user_id: dict[int, str]
    branch_id_by_user_id: dict[int, int]


class ManagerConfigError(ConfigError):
    pass


def _load_rows(data: dict[str, Any]) -> ManagerConfig:
    raw_source_ids = data.get("source_responsible_ids", [36, 44])
    if not isinstance(raw_source_ids, list):
        raise ManagerConfigError("source_responsible_ids must be a list")
    source_responsible_ids = [as_int(value, "source_responsible_ids") for value in raw_source_ids]

    user_names: dict[int, str] = {}
    branch_by_user_id: dict[int, str] = {}
    branch_id_by_user_id: dict[int, int] = {}

    for row in data.get("technical_users", []):
        if not isinstance(row, dict):
            raise ManagerConfigError("Each technical_users row must be a mapping")
        user_id = as_int(row.get("id"), "technical_users.id")
        user_names[user_id] = as_str(row.get("name"), f"technical_users[{user_id}].name")

    managers = data.get("managers", [])
    if not isinstance(managers, list):
        raise ManagerConfigError("managers must be a list")

    allowed_user_ids: list[int] = []
    seen: set[int] = set()

    for row in managers:
        if not isinstance(row, dict):
            raise ManagerConfigError("Each managers row must be a mapping")

        user_id = as_int(row.get("id"), "managers.id")
        if user_id in seen:
            raise ManagerConfigError(f"Duplicate manager id in managers.yml: {user_id}")
        seen.add(user_id)

        name = as_str(row.get("name"), f"managers[{user_id}].name")
        branch = as_str(row.get("branch"), f"managers[{user_id}].branch")
        branch_id = as_int(row.get("branch_id"), f"managers[{user_id}].branch_id")
        active = bool(row.get("active", True))

        user_names[user_id] = name
        branch_by_user_id[user_id] = branch
        branch_id_by_user_id[user_id] = branch_id

        if active:
            allowed_user_ids.append(user_id)

    if not allowed_user_ids:
        raise ManagerConfigError("No active managers in managers.yml")

    return ManagerConfig(
        allowed_user_ids=allowed_user_ids,
        user_names=user_names,
        source_responsible_ids=source_responsible_ids,
        branch_by_user_id=branch_by_user_id,
        branch_id_by_user_id=branch_id_by_user_id,
    )


def load_manager_config(path: str | Path | None = None) -> ManagerConfig:
    """Load active managers from config/managers.yml.

    Legacy fallback: eqazyna_bitrix/managers.yml is still accepted so older
    branches do not fail instantly, but new edits should be made in config/.
    """
    config_path = resolve_config_path("managers.yml", path)
    data = read_yaml(config_path)
    return _load_rows(data)
