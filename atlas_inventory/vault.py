"""Credential encryption at rest for CREDENTIALS.yml .

- AES-256-GCM from the `cryptography` library. No home-made crypto.
- Only values are encrypted; TAG and field names stay readable. Each value is bound to
  "<TAG>:<FIELD>" as associated data, so a value moved to another tag or field fails.
- Value format: ENC:v1:<base64url(nonce[12] + ciphertext + gcm tag[16])>
- The key (32 random bytes) lives outside DATA_PATH, the repo and OneDrive, mode 600.
  It is created only when no key exists AND the file holds no encrypted value; it is
  never overwritten. Optional split key: KEY_FILE xor KEY_FILE_2.
- get_credential() decrypts one tag for internal callers (the device server). It is never
  an MCP tool. Nothing here logs or prints a value, a username or the key.

CLI: python -m atlas_inventory.vault {status,init,encrypt,check,rekey}
"""
from __future__ import annotations

import argparse
import base64
import copy
import logging
import os
import sys
import time
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, consumers

log = logging.getLogger("atlas_inventory.vault")

ENC_PREFIX = "ENC:v1:"
KEY_LEN = 32
NONCE_LEN = 12
# Metadata stays readable (and keeps its YAML type); every other non-empty field is a secret,
# USERNAME and AUTH_USER included. Fields seen in the real NAVI CREDENTIALS.yml on 2026-11-10:
# TAG IS_ENABLED DESCRIPTION CACHE_TIMEOUT | USERNAME LOGIN_PASSWORD ENABLE_PASSWORD |
# HOST FROM PORT TLS AUTH_USER AUTH_PASS (a mail entry).
NON_SECRET_FIELDS = frozenset({"TAG", "IS_ENABLED", "DESCRIPTION", "TYPE", "CACHE_TIMEOUT",
                               "HOST", "FROM", "PORT", "TLS"})


class VaultError(Exception):
    """Base error. Messages name tags, fields and paths only, never values."""


class VaultKeyError(VaultError):
    pass


class VaultDecryptError(VaultError):
    pass


class Credential(dict):
    """One decrypted credential. repr/str never show values (a stray log line can't leak them)."""

    def __repr__(self) -> str:
        return f"Credential(fields={sorted(self)}, values=<redacted>)"

    __str__ = __repr__


# --------------------------------------------------------------------------- values
def _aad(tag: str, field: str) -> bytes:
    return f"atlas:v1:{tag}:{field}".encode()


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def _is_blank(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def encrypt_value(key: bytes, tag: str, field: str, plaintext: str) -> str:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, str(plaintext).encode(), _aad(tag, field))
    return ENC_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


def decrypt_value(key: bytes, tag: str, field: str, token: str) -> str:
    try:
        body = token[len(ENC_PREFIX):]
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return AESGCM(key).decrypt(raw[:NONCE_LEN], raw[NONCE_LEN:], _aad(tag, field)).decode()
    except (InvalidTag, ValueError, TypeError):
        raise VaultDecryptError(f"cannot decrypt {tag}.{field}: wrong key, damaged value, or a value "
                                f"copied from another tag/field") from None


# --------------------------------------------------------------------------- key
def key_parts(cfg: config.Config) -> list[Path]:
    return [cfg.key_file] + ([cfg.key_file_2] if cfg.key_file_2 else [])


def _check_key_path(cfg: config.Config, path: Path) -> None:
    p = path.expanduser().resolve(strict=False)
    if any("onedrive" in part.lower() for part in p.parts):
        raise VaultKeyError(f"key path {path} is under OneDrive; keep the key out of synced folders")
    for name, root in (("DATA_PATH", cfg.data_path), ("the repo", config.REPO)):
        if p.is_relative_to(Path(root).expanduser().resolve(strict=False)):
            raise VaultKeyError(f"key path {path} is inside {name}; keep the key apart from the data and code")


def _read_part(path: Path) -> bytes:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise VaultKeyError(f"key missing: {path}. Restore it from your backup; a new key cannot "
                            f"decrypt the stored credentials") from None
    if st.st_mode & 0o077:
        raise VaultKeyError(f"key file {path} has mode {oct(st.st_mode & 0o777)}; it must be 600 "
                            f"(chmod 600 {path})")
    data = path.read_bytes()
    if len(data) != KEY_LEN:
        raise VaultKeyError(f"key file {path} is {len(data)} bytes, expected {KEY_LEN}")
    return data


def _xor(parts: list[bytes]) -> bytes:
    out = bytearray(parts[0])
    for part in parts[1:]:
        out = bytearray(a ^ b for a, b in zip(out, part))
    return bytes(out)


def load_key(cfg: config.Config) -> bytes:
    """Read the key (XOR of the parts). Never creates one."""
    parts = key_parts(cfg)
    for p in parts:
        _check_key_path(cfg, p)
    return _xor([_read_part(p) for p in parts])


def _write_new(path: Path, data: bytes) -> None:
    """Create a file with mode 600. Fails if it exists (never overwrites)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(path: Path) -> None:
    if not path.exists():
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)


def create_key(cfg: config.Config) -> list[Path]:
    """First-run key creation. Refuses if any key part exists, or if encrypted values exist."""
    parts = key_parts(cfg)
    for p in parts:
        _check_key_path(cfg, p)
    present = [p for p in parts if p.exists()]
    if present and len(present) == len(parts):
        raise VaultKeyError(f"a key already exists ({', '.join(map(str, present))}); it is never overwritten")
    if present:
        missing = [str(p) for p in parts if not p.exists()]
        raise VaultKeyError(f"key part missing: {', '.join(missing)}. Restore it from your backup; making "
                            f"a new part would change the key")
    if has_encrypted(cfg.credential_file):
        raise VaultKeyError(f"no key, but {cfg.credential_file.name} holds encrypted values. Restore the key "
                            f"from your backup; a new key cannot decrypt them")
    for p in parts:
        _ensure_dir(p.parent)
        _write_new(p, os.urandom(KEY_LEN))
        log.info("created key part %s (mode 600)", p)
    return parts


def _key_for_write(cfg: config.Config) -> bytes:
    """Key for encrypting: the existing one, or a new one on a true first run."""
    if not any(p.exists() for p in key_parts(cfg)):
        create_key(cfg)
    return load_key(cfg)


# --------------------------------------------------------------------------- credential file
def _read_doc(path: Path) -> dict:
    if not path.is_file():
        raise VaultError(f"credential file {path} not found")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("DATA"), (list, dict)):
        raise VaultError(f"{path.name}: needs a top-level DATA: list (or mapping)")
    return raw


def _entries(raw: dict) -> list[tuple[str, dict]]:
    """(tag, entry) pairs. Entries are the live dicts inside raw, so edits change raw."""
    data = raw["DATA"]
    if isinstance(data, list):
        return [(str(e["TAG"]), e) for e in data if isinstance(e, dict) and e.get("TAG")]
    return [(str(t), e) for t, e in data.items() if isinstance(e, dict)]


def _secret_fields(entry: dict):
    for field, value in entry.items():
        if field not in NON_SECRET_FIELDS and not _is_blank(value):
            yield field, value


def has_encrypted(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        raw = _read_doc(path)
    except VaultError:
        return False
    return any(is_encrypted(v) for _, e in _entries(raw) for _, v in _secret_fields(e))


def counts(path: Path) -> dict[str, tuple[int, int]]:
    """tag -> (encrypted values, plain values)."""
    out = {}
    for tag, entry in _entries(_read_doc(path)):
        vals = [v for _, v in _secret_fields(entry)]
        enc = sum(1 for v in vals if is_encrypted(v))
        out[tag] = (enc, len(vals) - enc)
    return out


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _dump(raw: dict) -> bytes:
    return yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True).encode()


def _backup(path: Path) -> Path:
    stamp = _stamp()
    dst = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while dst.exists():
        dst = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    _write_new(dst, path.read_bytes())
    return dst


def _atomic_write(path: Path, raw: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    _write_new(tmp, _dump(raw))
    os.replace(tmp, path)


def _decrypt_all(key: bytes, raw: dict) -> dict[tuple[str, str], str]:
    out = {}
    for tag, entry in _entries(raw):
        for field, value in _secret_fields(entry):
            if is_encrypted(value):
                out[(tag, field)] = decrypt_value(key, tag, field, value)
    return out


def encrypt_file(cfg: config.Config) -> tuple[int, Path | None]:
    """Encrypt every plain secret value. Returns (values encrypted, backup path or None)."""
    path = cfg.credential_file
    raw = _read_doc(path)
    plain = [(t, f) for t, e in _entries(raw) for f, v in _secret_fields(e) if not is_encrypted(v)]
    if not plain:
        log.info("encrypt: nothing to do, no plain values")
        return 0, None
    key = _key_for_write(cfg)
    _decrypt_all(key, raw)   # existing values must open with this key; never mix keys in one file
    for tag, entry in _entries(raw):
        for field, value in list(_secret_fields(entry)):
            if not is_encrypted(value):
                entry[field] = encrypt_value(key, tag, field, str(value))
    backup = _backup(path)
    _atomic_write(path, raw)
    log.info("encrypt: %d value(s) in %d tag(s) encrypted; previous file kept as %s",
             len(plain), len({t for t, _ in plain}), backup.name)
    return len(plain), backup


def check(cfg: config.Config) -> list[tuple[str, bool, str]]:
    """Decrypt every value in memory; report per tag. Values are dropped at once."""
    raw = _read_doc(cfg.credential_file)
    key = load_key(cfg) if has_encrypted(cfg.credential_file) else None
    out = []
    for tag, entry in _entries(raw):
        bad, plain = [], 0
        for field, value in _secret_fields(entry):
            if not is_encrypted(value):
                plain += 1
                continue
            try:
                decrypt_value(key, tag, field, value)
            except VaultDecryptError:
                bad.append(field)
        detail = (f"FAILED fields {', '.join(bad)}" if bad else "ok") + (f", {plain} plain" if plain else "")
        out.append((tag, not bad, detail))
    return out


def get_credential(cfg: config.Config, tag: str, consumer: str | None = None) -> Credential:
    """One decrypted credential for internal use (device server). Not an MCP tool.

    Refuses tags the consumer may not use, unknown tags and disabled tags. Caches nothing."""
    who = (consumer or cfg.consumer).upper()
    if not consumers.allowed_tag(cfg, tag, who):
        raise PermissionError(f"consumer {who} may not use credential {tag}")
    found = dict(_entries(_read_doc(cfg.credential_file))).get(str(tag))
    if found is None:
        raise VaultError(f"credential {tag} not found")
    if found.get("IS_ENABLED") is False:
        raise VaultError(f"credential {tag} is disabled")
    fields = list(_secret_fields(found))
    key = load_key(cfg) if any(is_encrypted(v) for _, v in fields) else None
    cred = Credential({f: decrypt_value(key, str(tag), f, v) if is_encrypted(v) else str(v)
                       for f, v in fields})
    del key
    log.info("credential served tag=%s consumer=%s fields=%s", tag, who, ",".join(sorted(cred)))
    return cred


def rekey(cfg: config.Config) -> dict:
    """New key, re-encrypt every value. Old key and old file are kept until you delete them."""
    path = cfg.credential_file
    parts = key_parts(cfg)
    old_key = load_key(cfg)
    raw = _read_doc(path)
    values = _decrypt_all(old_key, raw)
    new_parts = [p.with_name(p.name + ".new") for p in parts]
    for n in new_parts:
        if n.exists():
            raise VaultKeyError(f"{n} exists: an earlier rekey did not finish. Check which key opens "
                                f"{path.name} (python -m atlas_inventory.vault check) before removing it")
    for n in new_parts:
        _write_new(n, os.urandom(KEY_LEN))
    tmp = path.with_name(f"{path.name}.tmp-rekey")
    try:
        new_key = _xor([n.read_bytes() for n in new_parts])
        new_raw = copy.deepcopy(raw)
        for tag, entry in _entries(new_raw):
            for field, _ in list(_secret_fields(entry)):
                if (tag, field) in values:
                    entry[field] = encrypt_value(new_key, tag, field, values[(tag, field)])
        if tmp.exists():
            tmp.unlink()
        _write_new(tmp, _dump(new_raw))
        if _decrypt_all(new_key, yaml.safe_load(tmp.read_text())) != values:
            raise VaultError("rekey verification failed; nothing changed")
    except Exception:
        for n in new_parts:
            n.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        raise
    values.clear()
    stamp = _stamp()
    backup = _backup(path)
    old_keys = []
    for p in parts:
        old = p.with_name(f"{p.name}.old-{stamp}")
        os.replace(p, old)
        old_keys.append(old)
    os.replace(tmp, path)
    for p, n in zip(parts, new_parts):
        os.replace(n, p)
    from . import recovery
    recovery.refresh_escrow(cfg)                    # the recovery key opens the new key too
    log.info("rekey: %s re-encrypted; old file %s; old key %s", path.name, backup.name,
             ", ".join(o.name for o in old_keys))
    return {"backup": backup, "old_keys": old_keys}


# --------------------------------------------------------------------------- CLI
def status_lines(cfg: config.Config) -> list[str]:
    lines = []
    for p in key_parts(cfg):
        if p.exists():
            mode = os.stat(p).st_mode & 0o777
            lines.append(f"key {p}: present, mode {oct(mode)}{'' if mode & 0o077 == 0 else '  (TOO OPEN)'}")
        else:
            lines.append(f"key {p}: MISSING")
    lines.append(f"split key: {'yes' if cfg.key_file_2 else 'no'}")
    if cfg.credential_file.is_file():
        tot_e = tot_p = 0
        for tag, (e, p) in sorted(counts(cfg.credential_file).items()):
            tot_e, tot_p = tot_e + e, tot_p + p
            lines.append(f"  {tag}: {e} encrypted, {p} plain")
        lines.append(f"{cfg.credential_file.name}: {tot_e} encrypted, {tot_p} plain value(s)")
    else:
        lines.append(f"{cfg.credential_file}: not found")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas-vault", description="ATLAS credential encryption. "
                                 "No command prints a secret.")
    ap.add_argument("command", choices=["status", "init", "encrypt", "check", "rekey"])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = config.load()
    try:
        if a.command == "status":
            print("\n".join(status_lines(cfg)))
        elif a.command == "init":
            made = create_key(cfg)
            print(f"created {', '.join(map(str, made))}. Now make its recovery key: "
                  f"navigator-setup recovery init (then navigator-setup backup).")
        elif a.command == "encrypt":
            n, backup = encrypt_file(cfg)
            print(f"{n} value(s) encrypted." + (f" Previous file: {backup} (plain text; delete it once "
                                                 f"you have checked)" if backup else ""))
        elif a.command == "check":
            results = check(cfg)
            for tag, ok, detail in results:
                print(f"{tag}: {detail}")
            return 0 if all(ok for _, ok, _ in results) else 1
        elif a.command == "rekey":
            r = rekey(cfg)
            print(f"rekeyed. Back up the new key now. Kept until you delete them: {r['backup']}, "
                  f"{', '.join(map(str, r['old_keys']))}")
    except (VaultError, PermissionError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
