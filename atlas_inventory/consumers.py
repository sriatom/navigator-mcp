"""Per-consumer allow lists (bootstrap CONSUMERS:).

- No CONSUMERS section at all: everything is allowed (phase 1 behaviour).
- Section present, consumer not listed: nothing is allowed.
- Patterns are case-sensitive shell globs matched against the inventory name (INVENTORY_X)
  or the credential tag.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from .config import Config


def _rules(cfg: Config, consumer: str | None) -> dict | None:
    if cfg.consumers is None:
        return None
    return cfg.consumers.get((consumer or cfg.consumer).upper(), {})


def _allowed(cfg: Config, kind: str, name: str, consumer: str | None) -> bool:
    rules = _rules(cfg, consumer)
    if rules is None:
        return True
    return any(fnmatchcase(str(name), pat) for pat in rules.get(kind, []))


def allowed_inventory(cfg: Config, name: str, consumer: str | None = None) -> bool:
    return _allowed(cfg, "INVENTORIES", name, consumer)


def allowed_tag(cfg: Config, tag: str, consumer: str | None = None) -> bool:
    return _allowed(cfg, "CREDENTIALS", tag, consumer)
