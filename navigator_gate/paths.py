"""Where the gate keeps its files and settings."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULT_PORTS = {"NAVIGATOR": 8791, "ATLAS": 8792, "RUDDER": 8793}
DEFAULT_BIND = "0.0.0.0"


def config_dir() -> Path:
    return Path(os.environ.get("NAVIGATOR_CONFIG_DIR") or "~/.config/navigator").expanduser()


def tls_dir() -> Path:
    return Path(os.environ.get("NAVIGATOR_TLS_DIR") or config_dir() / "tls").expanduser()


def users_file() -> Path:
    return Path(os.environ.get("NAVIGATOR_USERS") or config_dir() / "users.yml").expanduser()


def nav_file() -> Path:
    return Path(os.environ.get("NAVIGATOR_BOOTSTRAP") or config_dir() / "navigator.yml").expanduser()


def state_dir() -> Path:
    try:
        raw = yaml.safe_load(nav_file().read_text()) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    return Path(str(raw.get("STATE_PATH") or "~/.navigator")).expanduser()


def http_settings() -> dict:
    """navigator.yml HTTP block with defaults: {"BIND": ..., "PORTS": {...}}."""
    try:
        raw = yaml.safe_load(nav_file().read_text()) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    h = {str(k).upper(): v for k, v in (raw.get("HTTP") or {}).items()}
    ports = dict(DEFAULT_PORTS)
    ports.update({str(k).upper(): int(v) for k, v in (h.get("PORTS") or {}).items()})
    return {"BIND": str(h.get("BIND") or DEFAULT_BIND), "PORTS": ports}


def write_private(p: Path, data: bytes | str) -> None:
    """Write a file readable only by its owner, atomically."""
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
