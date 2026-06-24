from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import ConfigError, as_int, read_yaml, resolve_config_path


@dataclass(frozen=True)
class SpecialOwnerPinConfig:
    owner_by_contact_id: dict[int, int]


class SpecialOwnerPinConfigError(ConfigError):
    pass


def _load_rows(data: dict[str, Any]) -> SpecialOwnerPinConfig:
    rows = data.get("special_director_owner_pins", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise SpecialOwnerPinConfigError("special_director_owner_pins must be a list")

    owner_by_contact_id: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SpecialOwnerPinConfigError("Each special_director_owner_pins row must be a mapping")
        active = bool(row.get("active", True))
        if not active:
            continue
        contact_id = as_int(row.get("contact_id"), "special_director_owner_pins.contact_id")
        target_user_id = as_int(row.get("target_user_id"), f"special_director_owner_pins[{contact_id}].target_user_id")
        if contact_id in owner_by_contact_id and owner_by_contact_id[contact_id] != target_user_id:
            raise SpecialOwnerPinConfigError(f"Duplicate special pin for contact_id={contact_id}")
        owner_by_contact_id[contact_id] = target_user_id

    return SpecialOwnerPinConfig(owner_by_contact_id=owner_by_contact_id)


def load_special_owner_pin_config(path: str | Path | None = None) -> SpecialOwnerPinConfig:
    config_path = resolve_config_path("special_owner_pins.yml", path)
    try:
        data = read_yaml(config_path)
    except ConfigError:
        # Optional config: old deployments should not fail when the file is absent.
        return SpecialOwnerPinConfig(owner_by_contact_id={})
    return _load_rows(data)
