from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import ConfigError, as_int, as_str, read_yaml, resolve_config_path


def _normalize_bin(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) == 12 else ""


def load_hard_bin_owners_raw(path: str | Path | None = None) -> dict[int, list[str]]:
    config_path = resolve_config_path("hard_bins.yml", path)
    data = read_yaml(config_path)
    rows = data.get("hard_bin_owners", [])
    if not isinstance(rows, list):
        raise ConfigError("hard_bin_owners must be a list")

    result: dict[int, list[str]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"hard_bin_owners[{index}] must be a mapping")
        user_id = as_int(row.get("user_id"), f"hard_bin_owners[{index}].user_id")
        bins = row.get("bins", [])
        if not isinstance(bins, list):
            raise ConfigError(f"hard_bin_owners[{index}].bins must be a list")
        cleaned: list[str] = []
        for value in bins:
            bin_value = _normalize_bin(value)
            if not bin_value:
                raise ConfigError(f"Invalid BIN for user {user_id}: {value!r}")
            cleaned.append(bin_value)
        result[user_id] = cleaned
    return result


def build_hard_bin_owners(raw: dict[int, list[str]]) -> dict[str, list[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for user_id, bins in raw.items():
        for bin_value in bins:
            normalized = _normalize_bin(bin_value)
            if normalized:
                index[normalized].add(int(user_id))
    return {bin_value: sorted(owner_ids) for bin_value, owner_ids in index.items()}


def load_hard_bin_owners(path: str | Path | None = None) -> dict[str, list[int]]:
    return build_hard_bin_owners(load_hard_bin_owners_raw(path))


def load_manual_director_owners_raw(path: str | Path | None = None) -> dict[int, list[str]]:
    config_path = resolve_config_path("manual_directors.yml", path)
    data = read_yaml(config_path)
    rows = data.get("manual_director_owners", [])
    if not isinstance(rows, list):
        raise ConfigError("manual_director_owners must be a list")

    result: dict[int, list[str]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"manual_director_owners[{index}] must be a mapping")
        user_id = as_int(row.get("user_id"), f"manual_director_owners[{index}].user_id")
        directors = row.get("directors", [])
        if not isinstance(directors, list):
            raise ConfigError(f"manual_director_owners[{index}].directors must be a list")
        result[user_id] = [as_str(name, f"manual_director_owners[{index}].directors") for name in directors]
    return result
