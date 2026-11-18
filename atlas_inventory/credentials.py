"""Credential metadata. Nothing in this module returns or decrypts a secret value.

CREDENTIALS.yml has a top-level DATA: section, either
    DATA: [{TAG: X, TYPE: LOGIN, USERNAME: .., LOGIN_PASSWORD: ..}, ...]   (current)
or  DATA: {X: {USERNAME: .., LOGIN_PASSWORD: ..}}
Values may be plain or encrypted (ENC:v1:..., see vault.py).

Credential TYPEs and the secret fields each one needs ("a|b" = either):
    LOGIN      USERNAME, LOGIN_PASSWORD|PASSWORD        (optional ENABLE_PASSWORD)
    API_TOKEN  TOKEN|API_KEY                            (optional USERNAME)
    SNMP       COMMUNITY (v2c)  or  USERNAME + AUTH_PASS (v3, optional PRIV_PASS)
    CERT       CERT, PRIVATE_KEY                        (PEM text; optional PASSPHRASE)
    SMTP       AUTH_USER, AUTH_PASS                     (HOST, FROM, PORT, TLS stay readable)
A tag without TYPE gets one inferred from its fields; the listing says "(inferred)".
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .vault import NON_SECRET_FIELDS, is_encrypted

CREDENTIAL_TYPES: dict[str, list[list[tuple[str, ...]]]] = {
    # each type: list of alternative requirement sets; each set: fields, where a tuple = any one of
    "LOGIN": [[("USERNAME",), ("LOGIN_PASSWORD", "PASSWORD")]],
    "API_TOKEN": [[("TOKEN", "API_KEY")]],
    "SNMP": [[("COMMUNITY",)], [("USERNAME",), ("AUTH_PASS",)]],
    "CERT": [[("CERT",), ("PRIVATE_KEY",)]],
    "SMTP": [[("AUTH_USER",), ("AUTH_PASS",)]],
}


@dataclass(frozen=True)
class CredentialInfo:
    tag: str
    fields_set: tuple[str, ...]   # names of non-empty secret fields, e.g. ("LOGIN_PASSWORD", "USERNAME")
    modified: str                 # modification time of the credential file
    encrypted: int = 0            # how many of those fields are encrypted
    enabled: bool = True
    ctype: str = "LOGIN"
    inferred: bool = False        # True when TYPE was not set in the file
    missing: tuple[str, ...] = field(default_factory=tuple)   # required fields not set

    @property
    def storage(self) -> str:
        if not self.fields_set:
            return "empty"
        if self.encrypted == len(self.fields_set):
            return "encrypted"
        return "plain" if self.encrypted == 0 else "partly encrypted"


def _entries(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    data = raw.get("DATA") if isinstance(raw, dict) else None
    out: dict[str, dict] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("TAG"):
                out[str(item["TAG"])] = {k: v for k, v in item.items() if k != "TAG"}
    elif isinstance(data, dict):
        for tag, entry in data.items():
            out[str(tag)] = entry if isinstance(entry, dict) else {}
    return out


def tags(path: Path) -> set[str]:
    return set(_entries(path))


def infer_type(fields: set[str]) -> str:
    if fields & {"TOKEN", "API_KEY"}:
        return "API_TOKEN"
    if "COMMUNITY" in fields:
        return "SNMP"
    if fields & {"CERT", "PRIVATE_KEY"}:
        return "CERT"
    if fields & {"AUTH_USER", "AUTH_PASS"} and not fields & {"LOGIN_PASSWORD", "PASSWORD"}:
        return "SMTP" if "AUTH_USER" in fields else "SNMP"
    return "LOGIN"


def missing_fields(ctype: str, fields: set[str]) -> tuple[str, ...]:
    """Required fields not set, for the closest requirement set. Unknown types: nothing checked."""
    options = CREDENTIAL_TYPES.get(ctype)
    if not options:
        return ()
    best: list[str] | None = None
    for req in options:
        miss = ["|".join(alts) for alts in req if not set(alts) & fields]
        if best is None or len(miss) < len(best):
            best = miss
    return tuple(best or ())


def describe(path: Path) -> list[CredentialInfo]:
    mtime = (time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
             if path.is_file() else "-")
    out = []
    for t, e in sorted(_entries(path).items()):
        fields = {k: v for k, v in e.items()
                  if k not in NON_SECRET_FIELDS and v not in (None, "", [], {})}
        given = str(e.get("TYPE") or "").strip().upper()
        ctype = given or infer_type(set(fields))
        out.append(CredentialInfo(tag=t, fields_set=tuple(sorted(fields)), modified=mtime,
                                  encrypted=sum(1 for v in fields.values() if is_encrypted(v)),
                                  enabled=e.get("IS_ENABLED") is not False,
                                  ctype=ctype, inferred=not given,
                                  missing=missing_fields(ctype, set(fields))))
    return out
