"""Read a project's own files without a model: list them, and search them for text with the config block
around each hit. Built for real configs (hundreds of KB per device): the model gets the few lines that
answer the question, never whole files.

search("vlan 3102") in a folder of IOS / NX-OS / EOS configs returns, per device file, the blocks that
mention it, e.g. "interface Vlan3102" with its "ip address" lines and "vlan 3102" with its name.
"""
from __future__ import annotations

import re
from pathlib import Path

from .projects import OLD_NAMES, STATE_FILE

SKIP_DIRS = {"OUT"}                    # NAVIGATOR's own answers; searched only when named
SKIP_FILES = {STATE_FILE, OLD_NAMES[STATE_FILE]}
OWN_FILES = {"TASK.md", "RUNLOG.md"}   # NAVIGATOR's notes at the top; read only when named
TEXT_LIMIT = 8_000_000                 # bytes per file; bigger files are skipped and reported
BLOCK_LINES = 40                       # lines per block (parent line + its indented lines)


class ProjectFilesError(ValueError):
    pass


def _root(pdir: Path) -> Path:
    return pdir.resolve()


def _inside(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ProjectFilesError(f"{rel}: outside the project folder")
    return p


def _walk(root: Path, start: Path, named: bool) -> list[Path]:
    out = []
    for p in sorted(start.rglob("*")) if start.is_dir() else [start]:
        rel = p.relative_to(root).parts
        if not p.is_file() or any(x.startswith(".") for x in rel) or p.name in SKIP_FILES:
            continue
        if not named and (rel[0] in SKIP_DIRS or (len(rel) == 1 and p.name in OWN_FILES)):
            continue
        out.append(p)
    return out


def overview(pdir: Path) -> dict:
    """Folder -> file count (no file names): safe to show for any project mode."""
    root = _root(pdir)
    folders: dict[str, int] = {}
    for p in _walk(root, root, named=True):
        rel = p.relative_to(root).parts
        key = rel[0] + "/" if len(rel) > 1 else "(top)"
        folders[key] = folders.get(key, 0) + 1
    return folders


def pick(pdir: Path, files: list[str] | None) -> list[Path]:
    """The files to read: the named files / folders (inside the project), or every project file."""
    root = _root(pdir)
    if not files:
        return _walk(root, root, named=False)
    out: list[Path] = []
    for f in files:
        p = _inside(root, f)
        if not p.exists():
            have = ", ".join(f"{k} ({v} files)" for k, v in overview(pdir).items()) or "nothing"
            raise ProjectFilesError(f"{f}: not in the project. It has: {have}. "
                                    "Leave files out to use every file, or name one of those folders.")
        out += _walk(root, p, named=True)
    return out


def listing(pdir: Path, files: list[str] | None = None, limit: int = 200) -> dict:
    root = _root(pdir)
    paths = pick(pdir, files)
    rows = [{"file": str(p.relative_to(root)), "bytes": p.stat().st_size} for p in paths[:limit]]
    return {"files": rows, "count": len(paths), "shown": len(rows), "folders": overview(pdir)}


def _pattern(text: str, regex: bool) -> re.Pattern:
    if regex:
        try:
            return re.compile(text, re.I)
        except re.error as e:
            raise ProjectFilesError(f"search: not a valid regex ({e})") from None
    words = [re.escape(w) for w in text.split()]
    if not words:
        raise ProjectFilesError("search: give the text to look for")
    body = r"\s*".join(words)          # "vlan 3102" also finds "Vlan3102" (interface names)
    head = r"(?<![\w])" if re.match(r"\w", text.strip()) else ""
    tail = r"(?![\w])" if re.search(r"\w$", text.strip()) else ""
    return re.compile(head + body + tail, re.I)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _block(lines: list[str], i: int) -> tuple[int, list[str]]:
    """The config block around line i: its top-level parent and that parent's indented lines."""
    top = i
    while top > 0 and (_indent(lines[top]) > 0 or not lines[top].strip()):
        top -= 1
    if _indent(lines[top]) > 0 or not lines[top].strip() or lines[top].lstrip().startswith(("!", "#")):
        top = i                                        # no parent (flat file): the line itself
    end = top + 1
    while end < len(lines) and lines[end].strip() and _indent(lines[end]) > 0:
        end += 1
    if top == i and end == i + 1:                      # a flat line: show it alone
        return top, [lines[i]]
    blk = lines[top:end]
    if len(blk) > BLOCK_LINES:                         # long block: the parent, then lines near the hit
        near = max(top + 1, i - 5)
        blk = [lines[top], "  ..."] + lines[near:min(end, near + BLOCK_LINES - 3)] + (["  ..."] if near + BLOCK_LINES - 3 < end else [])
    return top, blk


def search(pdir: Path, text: str, files: list[str] | None = None, regex: bool = False,
           max_blocks: int = 60, max_chars: int = 30_000) -> dict:
    root = _root(pdir)
    rx = _pattern(text, regex)
    hits, per_file, skipped, chars, cut = [], {}, [], 0, False
    for p in pick(pdir, files):
        rel = str(p.relative_to(root))
        if p.stat().st_size > TEXT_LIMIT:
            skipped.append(f"{rel} (over {TEXT_LIMIT // 1_000_000} MB)")
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            skipped.append(f"{rel} (unreadable)")
            continue
        if b"\0" in raw[:4096]:
            continue                                   # binary
        lines = raw.decode("utf-8", errors="replace").splitlines()
        seen: set[int] = set()
        for i, ln in enumerate(lines):
            if not rx.search(ln):
                continue
            per_file[rel] = per_file.get(rel, 0) + 1
            top, blk = _block(lines, i)
            if top in seen:
                continue
            seen.add(top)
            body = "\n".join(blk)
            if len(hits) >= max_blocks or chars + len(body) > max_chars:
                cut = True
                continue
            hits.append({"file": rel, "line": i + 1, "block": body})
            chars += len(body)
    out = {"search": text, "regex": regex, "files_with_matches": per_file, "blocks": hits,
           "matches": sum(per_file.values())}
    if cut:
        out["cut"] = (f"only the first {len(hits)} blocks are shown: search for something narrower, "
                      "or name one file in files=")
    if skipped:
        out["skipped"] = skipped
    if not per_file:
        out["next"] = ("no match. Try fewer words, another spelling (e.g. 'Vlan3102' or 'vlan 3102' are the "
                       "same here), or regex=true.")
    return out
