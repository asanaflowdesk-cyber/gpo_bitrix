from __future__ import annotations

# Compatibility facade. New code should import from eqazyna_bitrix.config.managers.
from .config.managers import ManagerConfig, ManagerConfigError, load_manager_config

__all__ = ["ManagerConfig", "ManagerConfigError", "load_manager_config"]
