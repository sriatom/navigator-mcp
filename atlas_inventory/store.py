"""Versioned, audited writes for ATLAS's own files (inventories, credentials, consumers).

- Every write first copies the current file to STATE_PATH/versions/<file name>/<stamp>.yml.
- New content is written to a temp file (mode 600) and renamed into place.
- Every write appends one line to STATE_PATH/audit.log: when, who, what, summary. No values.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import config


class StoreError(Exception):
    pass


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def state_dir(cfg: config.Config, *parts: str) -> Path:
    """STATE_PATH (and the sub folders) with mode 700 at every level we own."""
    p = ensure_dir(cfg.state_path)
    for part in parts:
        p = ensure_dir(p / part)
    return p


def digest(path: Path) -> str | None:
    """sha256 of a file, or None if it does not exist (used to refuse stale drafts)."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def versions_dir(cfg: config.Config, path: Path) -> Path:
    return cfg.state_path / "versions" / path.name


def _write_600(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def audit(cfg: config.Config, who: str, action: str, summary: str) -> None:
    ensure_dir(cfg.state_path)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{who}\t{action}\t{summary.replace(chr(10), ' | ')}\n"
    fd = os.open(cfg.state_path / "audit.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def write_versioned(cfg: config.Config, path: Path, text: str, who: str, action: str, summary: str,
                    expect_digest: str | None | bool = False) -> Path | None:
    """Write text to path, keeping the previous content as a version. Returns the version path.

    expect_digest: the sha256 the file had when the change was drafted (None = file must not
    exist yet). False = do not check."""
    if expect_digest is not False and digest(path) != expect_digest:
        raise StoreError(f"{path.name} changed since this draft was made; start a new draft")
    kept = None
    if path.is_file():
        vdir = state_dir(cfg, "versions", path.name)
        st, n = stamp(), 0                 # <stamp>.<nnn>: names sort in time order
        kept = vdir / f"{st}.{n:03d}.yml"
        while kept.exists():
            n += 1
            kept = vdir / f"{st}.{n:03d}.yml"
        _write_600(kept, path.read_bytes())
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    _write_600(path, text.encode())
    audit(cfg, who, action, f"{path.name}: {summary}")
    return kept


def list_versions(cfg: config.Config, path: Path) -> list[Path]:
    vdir = versions_dir(cfg, path)
    return sorted(vdir.glob("*.yml"), reverse=True) if vdir.is_dir() else []


def read_version(cfg: config.Config, path: Path, version: str) -> str:
    vdir = versions_dir(cfg, path)
    name = version if version.endswith(".yml") else version + ".yml"
    target = (vdir / name).resolve()
    if target.parent != vdir.resolve() or not target.is_file():
        raise StoreError(f"version {version!r} of {path.name} not found")
    return target.read_text()
