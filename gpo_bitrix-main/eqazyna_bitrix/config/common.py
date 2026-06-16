from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = PACKAGE_DIR / "config"


class ConfigError(RuntimeError):
    """Raised when a YAML configuration file is missing or invalid."""


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return data


def resolve_config_path(filename: str, explicit_path: str | Path | None = None) -> Path:
    """Return the config path with a compatibility fallback for old flat files."""
    if explicit_path is not None:
        return Path(explicit_path)

    primary = CONFIG_DIR / filename
    if primary.exists():
        return primary

    legacy = PACKAGE_DIR / filename
    return legacy


def as_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid integer in config: {field_name}={value!r}") from exc


def as_str(value: Any, field_name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ConfigError(f"Empty value in config: {field_name}")
    return result
