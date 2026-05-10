"""TOML configuration loader with environment variable overrides."""
from __future__ import annotations
import os
import tomllib
from pathlib import Path
from typing import Any


def load_config(path: Path, env_prefix: str = "APP_") -> dict[str, Any]:
    """Load TOML config, then override leaf values from environment variables.

    Environment variable names are constructed as:
      {env_prefix}{SECTION}_{KEY}  (uppercased, dots replaced with underscores)
    """
    with path.open("rb") as fh:
        config: dict[str, Any] = tomllib.load(fh)

    _apply_env_overrides(config, env_prefix, prefix="")
    return config


def _apply_env_overrides(node: dict, env_prefix: str, prefix: str) -> None:
    for key, value in node.items():
        env_key = f"{env_prefix}{prefix}{key}".upper()
        if isinstance(value, dict):
            _apply_env_overrides(value, env_prefix, f"{prefix}{key}_")
        elif (env_val := os.environ.get(env_key)) is not None:
            # Cast to the original type
            if isinstance(value, bool):
                node[key] = env_val.lower() in ("1", "true", "yes")
            elif isinstance(value, int):
                node[key] = int(env_val)
            elif isinstance(value, float):
                node[key] = float(env_val)
            else:
                node[key] = env_val
