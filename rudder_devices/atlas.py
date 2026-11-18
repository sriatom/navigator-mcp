"""Client for the ATLAS link socket. RUDDER's only way to reach layer 1 (no file access, no imports)."""
from __future__ import annotations

import json
import socket

from .config import Settings


class AtlasError(Exception):
    pass


class Atlas:
    def __init__(self, s: Settings):
        self.s = s
        self._platforms: dict[str, dict] = {}
        self._commands: dict[str, dict] = {}

    def _token(self) -> str:
        try:
            return self.s.token_file.read_text().strip()
        except OSError:
            raise AtlasError(f"RUDDER's ATLAS token {self.s.token_file} is missing "
                             f"(create it on the host: atlas-link token RUDDER)") from None

    def call(self, op: str, **kw) -> dict:
        req = {"op": op, "token": self._token(), **kw}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
                sk.settimeout(30)
                sk.connect(str(self.s.atlas_socket))
                sk.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = sk.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except (FileNotFoundError, ConnectionRefusedError):
            raise AtlasError(f"ATLAS link is not running ({self.s.atlas_socket}); start it: atlas-link serve") from None
        except OSError as e:
            raise AtlasError(f"ATLAS link error: {type(e).__name__}") from None
        finally:
            req.clear()
        rep = json.loads(buf or b"{}")
        if not rep.get("ok"):
            raise AtlasError(rep.get("error") or "ATLAS link: empty reply")
        return rep

    def records(self, inventory: str, **sel) -> list[dict]:
        return self.call("records", consumer=self.s.consumer, inventory=inventory, **sel)["records"]

    def platform(self, name: str) -> dict:
        key = str(name).upper()
        if key not in self._platforms:
            self._platforms[key] = self.call("platform", name=name)["platform"]
        return self._platforms[key]

    def platforms(self) -> list[dict]:
        return self.call("platforms")["platforms"]

    def commands(self, platform_id: str) -> dict[str, list[str]]:
        if platform_id not in self._commands:
            self._commands[platform_id] = self.call("commands", platform=platform_id)["commands"]
        return self._commands[platform_id]

    def credential(self, tag: str) -> dict:
        """Decrypted fields. Callers drop them as soon as the connection is made."""
        return self.call("credential", consumer=self.s.consumer, tag=tag)["fields"]
