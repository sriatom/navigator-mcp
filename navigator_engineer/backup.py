"""Full backup and restore of a NAVIGATOR host: settings, the CREDENTIALS key, inventories, state.

  navigator-setup recovery init [--replace]   make the recovery key (and the CREDENTIALS key if none); shown once
  navigator-setup recovery check              type the recovery key: is it the right one?
  navigator-setup recovery refresh            re-seal the escrow after a rekey (rekey does it by itself)
  navigator-setup backup [--out DIR] [--no-records]
  navigator-setup restore FILE [--list] [--to DIR] [--yes]

A backup is ONE encrypted file (NAVIGATOR-BACKUP-<host>-<time>.nvbak, mode 600). It is sealed to the
recovery public key, and the backup command asks for the recovery key first and checks it, so a backup is
never made that its owner cannot open. Restore needs only the file and the recovery key (also on a new host).

What is in it (each only if it exists):
  NAVIGATOR settings dir (~/.config/navigator: navigator.yml, users.yml, tls/)   NAVIGATOR state (~/.navigator)
  RUDDER settings (~/.config/rudder: rudder.local.yml, atlas-link.token, approve.yml)  RUDDER state (~/.rudder:
  tickets, audit log, records; not delegated/ = installed software, not locks/)   the CREDENTIALS key dir
  (~/.config/atlas: vault.key, recovery.json)   ATLAS DATA_PATH (inventories + CREDENTIALS.yml, values encrypted)
  ATLAS state (~/.atlas)   an ATLAS bootstrap file outside the repo.
Not in it: project folders (PROJECT_ROOT: back them up with your normal file backups), the code
(reinstall with pip / uv), Hermes or other hosts' own settings.

File format: b"NAVBAK1\\n", a 4-byte header length, the header (JSON: host, time, sources, the recovery
public key's encrypted private half so a new host can open it, the file key sealed to the public key),
then chunks [flag 1][length 4][AES-256-GCM of 1 MiB of tar.gz]; the last chunk has flag 1, so a cut file
is detected. Each chunk is bound to the header and to its number.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import shutil
import socket
import struct
import sys
import tarfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"NAVBAK1\n"
CHUNK = 1 << 20
SKIP_DIRS = {"delegated", "locks", "__pycache__", ".venv", "node_modules"}


class BackupError(Exception):
    pass


# ------------------------------------------------------------------ what to back up
def _home_rel(p: Path) -> str:
    p = Path(p).expanduser()
    try:
        return "~/" + str(p.resolve(strict=False).relative_to(Path.home().resolve()))
    except ValueError:
        return str(p)


def sources(records: bool = True) -> list[dict]:
    """[{"label", "path" (~/... or absolute), "skip": [names]}], only what exists on this host."""
    from atlas_inventory import config as aconfig
    from rudder_devices import config as rconfig
    from navigator_gate.paths import config_dir, nav_file
    from . import config as nconfig
    out, seen = [], set()

    def add(label, p, skip=()):
        if not p:
            return
        p = Path(p).expanduser()
        key = str(p.resolve(strict=False))
        if not p.exists() or key in seen or any(key.startswith(s + os.sep) for s in seen):
            return
        seen.add(key)
        out.append({"label": label, "path": _home_rel(p), "skip": sorted(skip)})

    add("navigator-config", config_dir())
    add("navigator-bootstrap", nav_file())
    try:
        add("navigator-state", nconfig.load().state_path)
    except Exception:  # noqa: BLE001
        add("navigator-state", "~/.navigator")
    try:
        r = rconfig.load()
        add("rudder-config", r.token_file.parent)
        add("rudder-state", r.state_path, SKIP_DIRS | (set() if records else {"records"}))
        if records:
            add("rudder-records", r.records_path)
    except Exception:  # noqa: BLE001
        add("rudder-config", "~/.config/rudder")
        add("rudder-state", "~/.rudder", SKIP_DIRS | (set() if records else {"records"}))
    try:
        a = aconfig.load()
        add("atlas-key", a.key_file.parent)
        if a.key_file_2:
            add("atlas-key-2", a.key_file_2.parent)
        add("atlas-data", a.data_path)
        add("atlas-state", a.state_path)
        if a.bootstrap and not Path(a.bootstrap).resolve().is_relative_to(aconfig.REPO):
            add("atlas-bootstrap", a.bootstrap)
    except Exception:  # noqa: BLE001
        add("atlas-key", "~/.config/atlas")
        add("atlas-state", "~/.atlas")
    return out


# ------------------------------------------------------------------ encrypted stream
class _Writer(io.RawIOBase):
    def __init__(self, fh, key: bytes, bind: bytes):
        self.fh, self.aes, self.bind = fh, AESGCM(key), bind
        self.buf, self.n = bytearray(), 0
        self.prefix = os.urandom(8)

    def writable(self):
        return True

    def _emit(self, data: bytes, final: bool):
        flag = b"\x01" if final else b"\x00"
        nonce = self.prefix + struct.pack(">I", self.n)
        ct = self.aes.encrypt(nonce, bytes(data), self.bind + struct.pack(">I", self.n) + flag)
        self.fh.write(flag + struct.pack(">I", len(ct)) + nonce + ct)
        self.n += 1

    def write(self, b):
        self.buf += b
        while len(self.buf) >= CHUNK:
            self._emit(self.buf[:CHUNK], False)
            del self.buf[:CHUNK]
        return len(b)

    def finish(self):
        self._emit(self.buf, True)
        self.buf = bytearray()


class _Reader(io.RawIOBase):
    def __init__(self, fh, key: bytes, bind: bytes):
        self.fh, self.aes, self.bind = fh, AESGCM(key), bind
        self.buf, self.n, self.done = b"", 0, False

    def readable(self):
        return True

    def _next(self):
        head = self.fh.read(5)
        if len(head) < 5:
            raise BackupError("the backup file is cut short (no final chunk)")
        flag, ln = head[:1], struct.unpack(">I", head[1:])[0]
        nonce, ct = self.fh.read(12), self.fh.read(ln)
        if len(ct) != ln or nonce[8:] != struct.pack(">I", self.n):
            raise BackupError("the backup file is damaged")
        try:
            data = self.aes.decrypt(nonce, ct, self.bind + struct.pack(">I", self.n) + flag)
        except InvalidTag:
            raise BackupError("the backup file is damaged or was changed") from None
        self.n += 1
        if flag == b"\x01":
            self.done = True
            if self.fh.read(1):
                raise BackupError("data after the end of the backup")
        return data

    def readinto(self, b):
        while not self.buf and not self.done:
            self.buf = self._next()
        n = min(len(b), len(self.buf))
        b[:n] = self.buf[:n]
        self.buf = self.buf[n:]
        return n


# ------------------------------------------------------------------ backup
def create(recovery_key: str, out_dir: Path, records: bool = True, srcs: list[dict] | None = None) -> dict:
    from atlas_inventory import config as aconfig
    from atlas_inventory import recovery
    acfg = aconfig.load()
    recovery.check(acfg, recovery_key)                  # the user must hold the right recovery key
    doc = recovery.load_doc(acfg)
    srcs = srcs if srcs is not None else sources(records)
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = socket.gethostname()
    target = out_dir / f"NAVIGATOR-BACKUP-{host}-{stamp}.nvbak"
    file_key = os.urandom(32)
    header = {"FORMAT": 1, "HOST": host, "CREATED": time.strftime("%Y-%m-%d %H:%M:%S"),
              "SOURCES": [{"label": s["label"], "path": s["path"]} for s in srcs],
              "RECOVERY": {"PUBLIC": doc["PUBLIC"], "PRIVATE": doc["PRIVATE"]},
              "FILE_KEY": recovery.seal(doc["PUBLIC"], file_key, b"backup")}
    hb = json.dumps(header, sort_keys=True).encode()
    bind = hashlib.sha256(hb).digest()
    out_res = out_dir.resolve()
    files = 0
    tmp = target.with_name(target.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(MAGIC + struct.pack(">I", len(hb)) + hb)
            w = _Writer(fh, file_key, bind)
            with tarfile.open(fileobj=w, mode="w|gz") as tar:
                man = json.dumps({"SOURCES": header["SOURCES"], "HOST": host}).encode()
                ti = tarfile.TarInfo("MANIFEST.json")
                ti.size, ti.mtime, ti.mode = len(man), time.time(), 0o600
                tar.addfile(ti, io.BytesIO(man))
                for s in srcs:
                    base = Path(s["path"]).expanduser()
                    skip = set(s.get("skip") or ())

                    def filt(ti, base=base, skip=skip):
                        nonlocal files
                        rel = Path(ti.name).parts[1:]
                        if rel and rel[0] in skip:
                            return None
                        full = base.joinpath(*rel) if rel else base
                        try:
                            if full.resolve().is_relative_to(out_res):
                                return None
                        except OSError:
                            return None
                        if not (ti.isfile() or ti.isdir() or ti.issym()):
                            return None
                        ti.uname = ti.gname = ""
                        files += ti.isfile()
                        return ti
                    tar.add(str(base), arcname=s["label"], recursive=True, filter=filt)
            w.finish()
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"file": str(target), "bytes": target.stat().st_size, "files": files,
            "sources": [f"{s['label']} ({s['path']})" for s in srcs]}


# ------------------------------------------------------------------ restore
def _open(path: Path, recovery_key: str):
    from atlas_inventory import recovery
    fh = open(path, "rb")
    if fh.read(len(MAGIC)) != MAGIC:
        fh.close()
        raise BackupError(f"{path} is not a NAVIGATOR backup")
    (ln,) = struct.unpack(">I", fh.read(4))
    hb = fh.read(ln)
    header = json.loads(hb)
    priv = recovery.unlock(header["RECOVERY"], recovery_key)
    file_key = recovery.unseal(priv, header["FILE_KEY"], b"backup")
    return fh, header, file_key, hashlib.sha256(hb).digest(), len(MAGIC) + 4 + ln


def read(path: Path, recovery_key: str) -> dict:
    """Decrypt the whole file once (every chunk checked) and list what is in it. Writes nothing."""
    fh, header, key, bind, _ = _open(path, recovery_key)
    names, size = [], 0
    with fh, tarfile.open(fileobj=_Reader(fh, key, bind), mode="r|gz") as tar:
        for m in tar:
            if m.isfile():
                names.append(m.name)
                size += m.size
    return {"host": header["HOST"], "created": header["CREATED"], "sources": header["SOURCES"],
            "files": names, "bytes": size}


def restore(path: Path, recovery_key: str, to: Path | None = None) -> dict:
    """Verify, then write. to=DIR: everything under DIR/<label>/ (nothing else touched). Otherwise each source
    goes back to its own path; what is there now is moved aside to <path>.pre-restore-<time> first."""
    info = read(path, recovery_key)                     # pass 1: every chunk authenticated
    srcs = {s["label"]: Path(s["path"]).expanduser() for s in info["sources"]}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved, written = [], 0
    fh, header, key, bind, _ = _open(path, recovery_key)
    staged = Path(to).expanduser() if to else None
    if staged:
        staged.mkdir(parents=True, exist_ok=True, mode=0o700)
    with fh, tarfile.open(fileobj=_Reader(fh, key, bind), mode="r|gz") as tar:
        current = None
        for m in tar:
            if m.name == "MANIFEST.json":
                continue
            label, _, rest = m.name.partition("/")
            if label not in srcs:
                raise BackupError(f"unexpected entry {m.name}")
            dest = (staged / label) if staged else srcs[label]
            if label != current:
                current = label
                if not staged and dest.exists():
                    aside, n = dest.with_name(f"{dest.name}.pre-restore-{stamp}"), 1
                    while aside.exists():                # never on top of an earlier copy
                        n += 1
                        aside = dest.with_name(f"{dest.name}.pre-restore-{stamp}-{n}")
                    os.replace(dest, aside)
                    moved.append(str(aside))
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rest:
                m.name = rest
                tar.extract(m, path=str(dest), filter="data")
            else:                                        # the source itself (a file, or the top dir)
                if m.isdir():
                    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                m.name = dest.name
                tar.extract(m, path=str(dest.parent), filter="data")
            written += m.isfile()
    return {**{k: info[k] for k in ("host", "created")}, "files_written": written, "moved_aside": moved,
            "to": str(staged) if staged else None}


# ------------------------------------------------------------------ CLI
def _ask_key(prompt="Recovery key (48 digits): ", ask=getpass.getpass) -> str:
    return ask(prompt).strip()


def main(argv: list[str], out=print, ask=getpass.getpass, confirm=input) -> int:
    from atlas_inventory import config as aconfig
    from atlas_inventory import recovery
    ap = argparse.ArgumentParser(prog="navigator-setup", description="recovery key, backup and restore")
    sub = ap.add_subparsers(dest="area", required=True)
    r = sub.add_parser("recovery").add_subparsers(dest="cmd", required=True)
    r.add_parser("init").add_argument("--replace", action="store_true")
    r.add_parser("check")
    r.add_parser("refresh")
    b = sub.add_parser("backup")
    b.add_argument("--out", default="~/navigator-backups")
    b.add_argument("--no-records", action="store_true", help="leave RUDDER's command records out")
    s = sub.add_parser("restore")
    s.add_argument("file")
    s.add_argument("--list", action="store_true", help="check the file and list it; write nothing")
    s.add_argument("--to", help="write everything under this folder instead of the original places")
    s.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.area == "recovery":
            cfg = aconfig.load()
            if a.cmd == "init":
                rk = recovery.init(cfg, replace=a.replace)
                out("\nRecovery key (shown ONCE; NAVIGATOR does not keep it):\n\n    " + rk + "\n")
                out("Write it down or put it in your password manager. You need it for every backup and restore,")
                out("and to open the CREDENTIALS key on a new host. Without it a backup cannot be opened.\n")
                for _ in range(3):
                    got = _ask_key("Type the recovery key back to confirm you saved it: ", ask)
                    try:
                        if recovery.parse(got) == recovery.parse(rk):
                            out("Confirmed.")
                            break
                    except recovery.RecoveryError as e:
                        out(f"  {e}")
                        continue
                    out("  That is not the same key; try again.")
                else:
                    out("Not confirmed. The key is saved; check it later with: navigator-setup recovery check")
                _log("recovery key made" + (" (replaced the old one)" if a.replace else ""))
            elif a.cmd == "check":
                recovery.check(cfg, _ask_key(ask=ask))
                out("Right recovery key: it opens the CREDENTIALS key in use.")
            else:
                out("escrow refreshed" if recovery.refresh_escrow(cfg) else "no recovery key yet")
        elif a.area == "backup":
            rk = _ask_key("Recovery key (the backup is encrypted for it): ", ask)
            res = create(rk, Path(a.out), records=not a.no_records)
            _log(f"backup {res['file']} ({res['files']} files)")
            out(f"backup: {res['file']}  ({res['bytes']:,} bytes, {res['files']} files)")
            for x in res["sources"]:
                out(f"  {x}")
            out("Copy it off this host (OneDrive is fine: it is encrypted). Project folders are not in it.")
        else:
            rk = _ask_key(ask=ask)
            info = read(Path(a.file), rk)
            out(f"backup of {info['host']}, {info['created']}: {len(info['files'])} files, {info['bytes']:,} bytes")
            for s in info["sources"]:
                out(f"  {s['label']:20} -> {s['path']}")
            if a.list:
                return 0
            if not a.to:
                out("Stop the services first: systemctl --user stop atlas-link rudder-scheduler hermes-gateway "
                    "navigator-mcp-https atlas-mcp-https rudder-mcp-https")
                if not a.yes and confirm("Restore to these places? What is there now is moved aside. yes/no: "
                                         ).strip().lower() not in ("y", "yes"):
                    out("Nothing restored.")
                    return 1
            res = restore(Path(a.file), rk, Path(a.to) if a.to else None)
            _log(f"restore {a.file} -> {res['to'] or 'original places'} ({res['files_written']} files)")
            out(f"restored {res['files_written']} files" + (f" under {res['to']}" if res["to"] else ""))
            for m in res["moved_aside"]:
                out(f"  kept the old one: {m}")
            if not a.to:
                out("Then: systemctl --user start atlas-link rudder-scheduler hermes-gateway (and the *-https ones "
                    "you use); check with: navigator-setup recovery check")
    except (recovery.RecoveryError, BackupError, FileNotFoundError) as e:
        print(f"navigator-setup: {e}", file=sys.stderr)
        return 2
    return 0


def _log(line: str) -> None:
    from navigator_gate.paths import state_dir
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(d / "settings.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{getpass.getuser()}\t{line}\n")
    except OSError:
        pass
