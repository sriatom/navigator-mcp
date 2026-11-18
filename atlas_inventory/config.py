"""Paths and settings for ATLAS, read from bootstrap.yml (paths and names only, no secrets).

Lookup order for the bootstrap file: $ATLAS_BOOTSTRAP, then bootstrap.yml next to the repo root.
Environment overrides: ATLAS_MODE, ATLAS_CONSUMER. The older NAVI_* names are still accepted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_KEY_FILE = "~/.config/atlas/vault.key"
DEFAULT_STATE_PATH = "~/.atlas"


def env(name: str) -> str | None:
    """ATLAS_<name>, falling back to the older NAVI_<name> / NAVI_MCP_<name>."""
    for var in (f"ATLAS_{name}", f"NAVI_{name}", f"NAVI_MCP_{name}"):
        if os.environ.get(var):
            return os.environ[var]
    return None


@dataclass(frozen=True)
class Config:
    data_path: Path          # private folder: inventories + CREDENTIALS.yml
    credential_file: Path
    state_path: Path         # ATLAS's own state (versions, one-time links)
    mode: str                # OPEN or LOCKED (which instance this is)
    key_file: Path           # credential encryption key (part 1 when split)
    key_file_2: Path | None  # optional second key part (key = part1 XOR part2)
    consumer: str            # who is calling: NAVI, LAB_BOT, ...
    consumers: dict | None   # CONSUMERS: allow lists; None = no section = everything allowed
    catalog_path: Path       # non-secret catalogues: PLATFORMS.yml, COMMANDS.yml
    consumers_file: Path     # written by the consumer_access interview; overrides bootstrap CONSUMERS
    netbox: dict | None      # NETBOX: {URL, TOKEN_CREDENTIAL, VERIFY_TLS}; None = not configured
    bootstrap: Path | None = None


def _path(value) -> Path:
    return Path(os.path.expanduser(str(value)))


def _consumers(raw) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("CONSUMERS must be a mapping of consumer name -> {INVENTORIES, CREDENTIALS}")
    out: dict[str, dict[str, list[str]]] = {}
    for name, rules in raw.items():
        rules = rules or {}
        if not isinstance(rules, dict):
            raise ValueError(f"CONSUMERS.{name} must be a mapping")
        entry = {}
        for key in ("INVENTORIES", "CREDENTIALS"):
            vals = rules.get(key) or []
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise ValueError(f"CONSUMERS.{name}.{key} must be a list of names or globs")
            entry[key] = vals
        out[str(name).upper()] = entry
    return out


def raw_bootstrap(cfg: Config) -> dict:
    """The bootstrap file as a mapping (for sections other modules own, e.g. LINK)."""
    return (yaml.safe_load(cfg.bootstrap.read_text()) or {}) if cfg.bootstrap else {}


def load() -> Config:
    boot = Path(env("BOOTSTRAP") or REPO / "bootstrap.yml")
    raw = yaml.safe_load(boot.read_text()) or {}
    data = _path(raw["DATA_PATH"])
    mode = (env("MODE") or str(raw.get("MODE", "OPEN"))).upper()
    if mode not in ("OPEN", "LOCKED"):
        raise ValueError(f"ATLAS_MODE must be OPEN or LOCKED, not {mode!r}")
    key2 = raw.get("KEY_FILE_2")
    state = _path(raw.get("STATE_PATH", DEFAULT_STATE_PATH))
    consumers_file = _path(raw.get("CONSUMERS_FILE") or state / "consumers.yml")
    consumers_raw = raw.get("CONSUMERS")
    if consumers_file.is_file():   # written by the consumer_access interview; wins over bootstrap
        consumers_raw = (yaml.safe_load(consumers_file.read_text()) or {}).get("CONSUMERS")
    netbox = raw.get("NETBOX")
    if netbox is not None and not (isinstance(netbox, dict) and netbox.get("URL")):
        raise ValueError("NETBOX must be a mapping with URL and TOKEN_CREDENTIAL")
    return Config(data_path=data,
                  credential_file=data / str(raw.get("CREDENTIAL_FILE", "CREDENTIALS.yml")),
                  state_path=state,
                  mode=mode,
                  key_file=_path(raw.get("KEY_FILE") or DEFAULT_KEY_FILE),
                  key_file_2=_path(key2) if key2 else None,
                  consumer=str(env("CONSUMER") or raw.get("CONSUMER") or "NAVI").upper(),
                  consumers=_consumers(consumers_raw),
                  catalog_path=_path(raw.get("CATALOG_PATH") or Path(__file__).parent / "catalog"),
                  consumers_file=consumers_file,
                  netbox=netbox,
                  bootstrap=boot)
