"""Every command output on disk, like nv-collect: one file per device per command, plus run.json.

    RECORDS_PATH/<YYYYmmdd-HHMMSS>-<id>/
        run.json                         who, what, when, per-device status
        <DEVICE>/<nn>_<command-slug>.json|.txt
Files are mode 600 and folders 700: outputs can hold configuration.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path


def _dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p, 0o700)
    return p


def _write(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def slug(cmd: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", cmd.lower()).strip("-")[:80] or "cmd"


class Run:
    def __init__(self, root: Path, meta: dict):
        _dir(root)
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.path = _dir(root / self.id)
        self.meta = {"run": self.id, "started": time.strftime("%Y-%m-%d %H:%M:%S"), **meta, "devices": {}}

    def save(self, device: str, results: list[dict]) -> list[str]:
        ddir = _dir(self.path / re.sub(r"[^A-Za-z0-9._-]", "_", device))
        paths = []
        for i, r in enumerate(results, 1):
            ext = "json" if r["format"] == "json" else "txt"
            p = ddir / f"{i:02d}_{slug(r['command'])}.{ext}"
            if r["output"] is None:
                body = f"# command failed: {r.get('error')}\n"
            elif r["format"] == "json":
                body = json.dumps(r["output"], indent=2)
            else:
                body = str(r["output"])
            _write(p, body)
            paths.append(str(p))
        return paths

    def finish(self, device_status: dict) -> Path:
        self.meta["devices"] = device_status
        self.meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        p = self.path / "run.json"
        _write(p, json.dumps(self.meta, indent=2))
        return p
