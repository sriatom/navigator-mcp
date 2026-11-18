"""Scrambler API: sanitize / restore for project files or a piece of text.

Maps (real <-> fake) live privately in SCRAMBLER_MAPS/<project>/map.json (mode 600), never next to the
sanitized files; denylist terms in SCRAMBLER_MAPS/denylist.txt (all projects) and <project>/denylist.txt.
Every result is checked for leaks (fail-closed): a run with findings is written to SANITIZED/<ts>-FAILED and
sanitize_text then returns no text at all.
"""
from __future__ import annotations

import re

import ipaddress
import os
from datetime import datetime
from pathlib import Path

from ..config import Settings, state_dir
from ..projects import ProjectError, Projects
from . import engine as E

NOTE_DEFAULT = """NOTE FOR THE AI MODEL — secrets in these files were removed before upload.
- Placeholders like <PASSWORD type=7>, <KEY>, <PSK>, <SNMP-COMMUNITY>, <PEM-REDACTED> replace removed
  secrets. Do not invent values for them; refer to them by placeholder.
"""


_OUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

class ScrambleError(Exception):
    pass


def _clean_terms(terms) -> list[str]:
    out = []
    for t in terms or []:
        t = " ".join(str(t).split())
        if len(t) < 3:
            raise ScrambleError(f"denylist term {t!r} is too short (3 characters or more)")
        out.append(t)
    return out


class Scrambler:
    def __init__(self, s: Settings):
        self.s = s
        self.projects = Projects(s)

    # ------------------------------------------------------------------ helpers
    def _pvault(self, project: str) -> Path:
        return self.s.maps_path / project

    def _options(self, mode, level, map_private, keep_descriptions, keep_nets) -> E.Options:
        if mode not in E.MODES:
            raise ScrambleError(f"mode must be one of {', '.join(E.MODES)}")
        if level not in ("standard", "strict"):
            raise ScrambleError("level must be standard or strict")
        nets = list(self.s.keep_nets) + list(keep_nets or [])
        for n in nets:
            try:
                ipaddress.ip_network(n, strict=False)
            except ValueError:
                raise ScrambleError(f"keep_net {n!r} is not a CIDR") from None
        return E.Options(level=level, map_private=map_private, keep_descriptions=keep_descriptions,
                         keep_nets=nets, mode=mode)

    def _mapping(self, project: str | None, opt: E.Options, corpus: str, terms: list[str]) -> E.Mapping:
        if opt.mode == "default" or not project:
            mp = E.Mapping(os.devnull + ".none")          # secrets are never mapped: nothing to keep
        else:
            pv = state_dir(self._pvault(project))
            state_dir(self.s.maps_path)
            mp = E.Mapping(str(pv / "map.json"))
            if terms:
                fd = os.open(pv / "denylist.txt", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, "".join(t + "\n" for t in terms).encode())
                finally:
                    os.close(fd)
            for t in E.load_terms([str(self.s.maps_path / "denylist.txt"), str(pv / "denylist.txt")]):
                mp.name("term", t)
        mp.corpus_lower = corpus.lower()
        for m in E.IPV4_RE.finditer(corpus):
            o = m.group(1).split(".")
            mp.seen_v4_24.add(".".join(o[:3]))
            mp.seen_v4_16.add(".".join(o[:2]))
        for m in E.IPV6_RE.finditer(corpus):
            try:
                mp.seen_v6_48.add(ipaddress.IPv6Address(m.group(1)).exploded.replace(":", "")[:12])
            except ValueError:
                pass
        return mp

    def _run(self, files: list[tuple[str, str]], project, opt, terms):
        for rel, t in files:
            if E.S_OPEN in t or E.S_CLOSE in t:
                raise ScrambleError(f"{rel} contains reserved private-use characters; cannot sanitize safely")
        mp = self._mapping(project, opt, "\n".join(t for _, t in files), terms)
        mids, stats = [], {}
        for rel, t in files:
            tok = E.Tokens()
            mids.append((rel, E.phase_a(t, mp, tok, opt), tok))
        name_rx, term_rx = E.build_name_regex(mp), E.build_term_regex(mp)
        outs = []
        for rel, mid, tok in mids:
            outs.append((rel, E.phase_b(mid, mp, tok, opt, name_rx, term_rx)))
            for k, v in tok.stats.items():
                stats[k] = stats.get(k, 0) + v
        findings = [(rel, ln, kind, val) for rel, txt in outs for ln, kind, val in E.verify_text(txt, mp, opt, name_rx, term_rx)]
        if opt.mode == "deep" and project:
            mp.save()
            try:
                os.chmod(mp.path, 0o600)
            except OSError:
                pass
        return outs, stats, findings

    @staticmethod
    def _new_dir(base: Path, name: str) -> Path:
        """A folder name not used yet (never overwrite): <name>, then <name>-2, -3, ..."""
        p, n = base / name, 1
        while p.exists():
            n += 1
            p = base / f"{name}-{n}"
        return p

    @staticmethod
    def _inside(pdir: Path, rel: str) -> Path:
        p = (pdir / rel).resolve()
        if p != pdir.resolve() and pdir.resolve() not in p.parents:
            raise ScrambleError(f"{rel} is outside the project folder")
        return p

    # ------------------------------------------------------------------ tools
    def sanitize(self, project: str, inp: str = "RAW", mode: str = "default", terms=None, level: str = "standard",
                 map_private: bool = False, keep_descriptions: bool = False, keep_nets=None,
                 name: str | None = None) -> dict:
        pdir = self.projects.find(project)
        src = self._inside(pdir, inp)
        if not src.exists():
            raise ScrambleError(f"{inp} not found in project {project}")
        if name is not None:
            name = str(name).strip()
            if not _OUT_NAME.match(name):
                raise ScrambleError("name: a plain file name (letters, digits, . _ -), e.g. QALEAF1A_SANITIZED.txt")
            if not src.is_file():
                raise ScrambleError("name works only when inp is one file")
        opt = self._options(mode, level, map_private, keep_descriptions, keep_nets)
        files, skipped = [], []
        for rel, p in E.read_files(str(src)):
            if rel.startswith("_"):
                continue
            t = E.load_text(p)
            if t is None:
                skipped.append(rel)
            else:
                files.append((rel, t))
        if not files:
            raise ScrambleError(f"no text files in {inp}")
        outs, stats, findings = self._run(files, project, opt, _clean_terms(terms))
        if name:
            outs = [(name, txt) for _, txt in outs]
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = self._new_dir(pdir / "SANITIZED", f"{ts}-{mode}" + ("-FAILED" if findings else ""))
        for rel, txt in outs:
            E.write_text(str(out_dir / rel), txt)
        E.write_text(str(out_dir / "_MODEL_NOTE.txt"), E.MODEL_NOTE if mode == "deep" else NOTE_DEFAULT)
        rep = [f"# Scrambler report — {ts}", "", f"- Project: `{project}`", f"- Input: `{inp}`",
               f"- Mode: {mode}" + (f"; level {level}; map private IPs: {map_private}; keep descriptions: "
                                    f"{keep_descriptions}" if mode == "deep" else " (secrets only)"),
               f"- Files: {len(outs)}" + (f"; skipped (binary): {', '.join(skipped)}" if skipped else ""),
               f"- Verify: {'FAILED — ' + str(len(findings)) + ' finding(s)' if findings else 'PASS'}", "",
               "| Category | Replacements |", "| :--- | ---: |"] + [f"| {k} | {v} |" for k, v in sorted(stats.items())]
        if findings:
            rep += ["", "## Verify findings (values masked)", "", "| File | Line | Kind | Value |", "| :--- | ---: | :--- | :--- |"]
            rep += [f"| {f} | {ln} | {k} | `{v}` |" for f, ln, k, v in findings[:500]]
        E.write_text(str(out_dir / "_SANITIZE_REPORT.md"), "\n".join(rep) + "\n")
        self.projects.log(pdir, "SCRAMBLER", f"sanitize {inp} (mode {mode})",
                          f"{'FAILED' if findings else 'PASS'}: {out_dir.name}")
        return {"output": str(out_dir), "mode": mode, "files": len(outs), "skipped": skipped, "replaced": stats,
                "verify": "FAILED" if findings else "PASS",
                "findings": [{"file": f, "line": ln, "kind": k, "value": v} for f, ln, k, v in findings[:20]],
                "next": ("Do NOT upload: something real survived. Add denylist terms or use level strict, then run "
                         "again." if findings else
                         "Safe to send the files in output to a public model, with the text of _MODEL_NOTE.txt.")}

    def sanitize_text(self, text: str, mode: str = "default", project: str | None = None, terms=None,
                      level: str = "standard", map_private: bool = False, keep_descriptions: bool = False) -> dict:
        if mode == "deep" and not project:
            raise ScrambleError("deep mode needs a project (its map makes the answer restorable)")
        if project:
            self.projects.find(project)
        opt = self._options(mode, level, map_private, keep_descriptions, None)
        outs, stats, findings = self._run([("text", text or "")], project, opt, _clean_terms(terms))
        if findings:
            return {"verify": "FAILED", "text": None, "replaced": stats,
                    "findings": [{"line": ln, "kind": k, "value": v} for _, ln, k, v in findings[:20]],
                    "next": "Not returned: something real survived. Add denylist terms or use level strict."}
        return {"verify": "PASS", "text": outs[0][1], "replaced": stats, "mode": mode,
                "model_note": E.MODEL_NOTE if mode == "deep" else NOTE_DEFAULT}

    def restore(self, project: str, text: str | None = None, inp: str | None = None) -> dict:
        pdir = self.projects.find(project)
        mp_path = self._pvault(project) / "map.json"
        if not mp_path.is_file():
            raise ScrambleError(f"project {project} has no scrambler map (nothing was sanitized in deep mode)")
        mp = E.Mapping(str(mp_path))
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = self._new_dir(pdir / "RESTORED", ts)
        locked = self.projects.mode(pdir) == "LOCAL"
        if text is not None:
            restored = E.restore_text(text, mp)
            E.write_text(str(out_dir / "answer_sanitized.txt"), text)
            E.write_text(str(out_dir / "answer_restored.txt"), restored)
            self.projects.log(pdir, "SCRAMBLER", "restore (text)", f"OK: RESTORED/{ts}")
            return {"output": str(out_dir), "text": None if locked else restored,
                    "note": ("LOCAL project: the restored text is only in the file, for the local model."
                             if locked else "Removed secrets stay placeholders; they are never stored.")}
        if not inp:
            raise ScrambleError("give text or inp (a file or folder in the project)")
        src = self._inside(pdir, inp)
        n = 0
        for rel, p in E.read_files(str(src)):
            t = E.load_text(p)
            if t is None:
                continue
            E.write_text(str(out_dir / rel), E.restore_text(t, mp))
            n += 1
        self.projects.log(pdir, "SCRAMBLER", f"restore {inp}", f"OK: {n} file(s) in RESTORED/{ts}")
        return {"output": str(out_dir), "files": n,
                "note": "Removed secrets stay placeholders (<PASSWORD ...>); they are never stored."}

    def map_counts(self, project: str) -> dict:
        self.projects.find(project)
        p = self._pvault(project) / "map.json"
        if not p.is_file():
            return {"project": project, "entries": {}}
        d = E.Mapping(str(p)).d
        counts = {cat: len(v) for cat, v in d["entries"].items()}
        for k in ("ipv4", "ipv4_private", "ipv6", "asn", "mac"):
            if d.get(k):
                counts[k] = len(d[k])
        return {"project": project, "entries": counts}


__all__ = ["Scrambler", "ScrambleError", "ProjectError"]
