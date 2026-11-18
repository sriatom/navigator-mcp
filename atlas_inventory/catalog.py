"""Non-secret catalogues: platforms (PLATFORMS.yml, generated, + the enabled vendor packs) and the read
command lists (COMMANDS.yml + the packs' commands.yml).

Platform names are matched loosely: case, '-', '_' and spaces are ignored, and ids, aliases and
netmiko device_types all resolve to the same platform id (e.g. ARISTA_EOS, arista_eos, eos -> EOS).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml


class CatalogError(Exception):
    pass


def _norm(name) -> str:
    return re.sub(r"[-_ ]", "", str(name or "")).upper()


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict:   # mtime in the key: edits are picked up
    return yaml.safe_load(Path(path).read_text()) or {}


def _read(path: Path) -> dict:
    if not path.is_file():
        raise CatalogError(f"{path.name} not found in {path.parent}")
    return _load(str(path), path.stat().st_mtime)


class Catalog:
    def __init__(self, catalog_path: Path):
        self.path = catalog_path
        raw = _read(catalog_path / "PLATFORMS.yml")
        self.platforms: dict[str, dict] = dict(raw.get("PLATFORMS") or {})
        import navigator_packs
        if "generic" not in navigator_packs.enabled():           # the netmiko fallback is the generic pack
            self.platforms = {k: v for k, v in self.platforms.items() if v.get("source") != "netmiko"}
        self.platforms.update(navigator_packs.platforms())      # enabled vendor packs win
        self.controllers: dict[str, dict] = raw.get("CONTROLLER_PRODUCTS") or {}
        self.oob_types: dict[str, dict] = raw.get("OOB_TYPES") or {}
        self.netmiko_version = raw.get("NETMIKO_VERSION")
        self._index: dict[str, str] = {}
        # netmiko names first, then aliases, then ids, so an id always wins a clash
        for pid, p in self.platforms.items():
            if p.get("netmiko"):
                self._index.setdefault(_norm(p["netmiko"]), pid)
        for pid, p in self.platforms.items():
            for a in p.get("aliases") or []:
                self._index[_norm(a)] = pid
        for pid in self.platforms:
            self._index[_norm(pid)] = pid
        for key, pid in (raw.get("COMMAND_KEYS") or {}).items():
            self._index[_norm(key)] = pid

    # ------------------------------------------------------------------ platforms
    def resolve(self, name) -> str | None:
        """Any OS / platform / alias / netmiko name -> platform id, or None."""
        return self._index.get(_norm(name)) if name else None

    def platform(self, name) -> dict:
        pid = self.resolve(name)
        if not pid:
            raise CatalogError(f"unknown platform {name!r}. Use platform_list to see the names")
        return {"ID": pid, **self.platforms[pid]}

    def search(self, vendor: str | None = None, text: str | None = None,
               adapter: str | None = None) -> list[dict]:
        out = []
        for pid, p in sorted(self.platforms.items()):
            if vendor and _norm(vendor) != _norm(p.get("vendor")):
                continue
            if adapter and adapter.lower() not in f"{p.get('reads')} {p.get('writes')}".lower():
                continue
            if text:
                hay = " ".join([pid, str(p.get("vendor")), *map(str, p.get("aliases") or []),
                                str(p.get("netmiko") or "")]).upper()
                if text.upper() not in hay:
                    continue
            out.append({"ID": pid, **p})
        return out

    def controller(self, product) -> dict | None:
        for k, v in self.controllers.items():
            if _norm(k) == _norm(product):
                return {"PRODUCT": k, **v}
        return None

    def oob_type(self, typ) -> dict | None:
        for k, v in self.oob_types.items():
            if _norm(k) == _norm(typ):
                return {"TYPE": k, **v}
        return None

    # ------------------------------------------------------------------ commands
    def commands(self) -> dict[str, dict[str, list[str]]]:
        """COMMANDS.yml + the enabled vendor packs' commands.yml as {platform id: {category: [commands]}}.
        A platform listed in COMMANDS.yml uses that list (a host override); others use their pack's."""
        path = self.path / "COMMANDS.yml"
        out: dict[str, dict[str, list[str]]] = {}
        for key, cats in ((_read(path) or {}).items() if path.is_file() else []):
            if not isinstance(cats, dict):
                continue
            pid = self.resolve(key) or str(key)
            bucket = out.setdefault(pid, {})
            for cat, cmds in cats.items():
                if isinstance(cmds, list):
                    bucket.setdefault(str(cat), []).extend(str(c) for c in cmds)
        import navigator_packs
        for key, cats in navigator_packs.commands().items():          # the enabled vendor packs' lists,
            pid = self.resolve(key) or str(key)                        # unless COMMANDS.yml lists that
            if pid not in out:                                         # platform itself (a host override)
                out[pid] = {cat: list(cmds) for cat, cmds in cats.items()}
        return out

    def commands_for(self, platform) -> dict[str, list[str]]:
        pid = self.resolve(platform)
        if not pid:
            raise CatalogError(f"unknown platform {platform!r}")
        return self.commands().get(pid, {})


def describe_platform(p: dict) -> str:
    names = ", ".join(f"{k}={p[k]}" for k in ("netmiko", "napalm", "pyats", "ansible", "multivendor")
                      if p.get(k))
    extra = f"  aka {','.join(p['aliases'])}" if p.get("aliases") else ""
    return (f"{p['ID']}  {p.get('vendor')}  reads={p.get('reads')}  writes={p.get('writes')}"
            f"  conn={','.join(p.get('conn') or [])}" + (f"  [{names}]" if names else "") + extra)
