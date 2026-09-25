"""Environment loading for the standalone SQL translator service."""

from __future__ import annotations

import os
from pathlib import Path


def load_workspace_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if value and value[0:1] in {"'", '"'} and value[-1:] == value[0]:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


load_workspace_env()
