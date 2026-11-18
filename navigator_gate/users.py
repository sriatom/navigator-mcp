"""Users of the HTTP MCP servers: a name, a role (admin | engineer) and a password NAVIGATOR makes.

The password is random (32 characters), shown once when made, and stored only as an scrypt hash in
users.yml (mode 600). It is separate from the device credentials in ATLAS. Lost: `navigator-setup user reset`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

import yaml

from .paths import users_file, write_private

ROLES = ("admin", "engineer")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
N, R, P = 2 ** 14, 8, 1


class UserError(Exception):
    pass


def _load() -> dict:
    p = users_file()
    if not p.is_file():
        return {}
    d = yaml.safe_load(p.read_text()) or {}
    return dict(d.get("USERS") or {})


def _save(users: dict) -> None:
    write_private(users_file(), "# NAVIGATOR HTTP users (navigator-setup user ...). Hashes only.\n"
                  + yaml.safe_dump({"USERS": users}, sort_keys=True))


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def new_password() -> str:
    return secrets.token_urlsafe(24)               # 32 chars of A-Z a-z 0-9 - _ (never a dot)


def add(name: str, role: str) -> str:
    name, role = name.strip().lower(), role.strip().lower()
    if not NAME_RE.match(name):
        raise UserError("user name: 2-32 characters, a-z 0-9 _ -, starting with a letter")
    if role not in ROLES:
        raise UserError(f"role: one of {', '.join(ROLES)}")
    users = _load()
    if name in users:
        raise UserError(f"user {name} exists (reset its password with: navigator-setup user reset {name})")
    pw = new_password()
    users[name] = {"ROLE": role, "HASH": _hash(pw), "CREATED": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save(users)
    return pw


def reset(name: str) -> str:
    users = _load()
    if name not in users:
        raise UserError(f"no user {name}")
    pw = new_password()
    users[name]["HASH"] = _hash(pw)
    users[name]["RESET"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(users)
    return pw


def set_role(name: str, role: str) -> None:
    users = _load()
    if name not in users:
        raise UserError(f"no user {name}")
    if role not in ROLES:
        raise UserError(f"role: one of {', '.join(ROLES)}")
    users[name]["ROLE"] = role
    _save(users)


def remove(name: str) -> None:
    users = _load()
    if name not in users:
        raise UserError(f"no user {name}")
    del users[name]
    _save(users)


def listing() -> list[dict]:
    return [{"user": k, "role": v.get("ROLE"), "created": v.get("CREATED"), "reset": v.get("RESET")}
            for k, v in sorted(_load().items())]


def verify(name: str, password: str) -> str | None:
    """The user's role if the password is right, else None."""
    u = _load().get(name)
    if not u or not _check(password, str(u.get("HASH") or "")):
        _check(password, _DUMMY)                    # same work for unknown users
        return None
    role = str(u.get("ROLE") or "")
    return role if role in ROLES else None


_DUMMY = _hash("not-a-password", b"0123456789abcdef")
