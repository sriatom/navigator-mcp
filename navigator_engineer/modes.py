"""Project mode changes. CLOUD -> LOCAL at once. LOCAL -> CLOUD only after the user's out-of-band approval,
through the same channel and protocol RUDDER uses for device changes (/pushok on the approver's Telegram).

mode_set(project, "CLOUD") on a LOCAL project sends the request and returns waiting_approval; call it
again after the user approved and it switches the mode. A request expires after TTL_MIN (default 30)."""
from __future__ import annotations

import json
import os
import time

from rudder_devices import approval

from .config import Settings, state_dir
from .projects import CLOUD, LOCAL, ProjectError, Projects, norm_mode


class ModeChanger:
    def __init__(self, s: Settings, channel=None):
        self.s = s
        self.projects = Projects(s)
        self.channel = channel or approval.channel(s.approval, s.consumer)
        self.ttl = int((s.approval or {}).get("TTL_MIN", 30)) * 60
        self.dir = state_dir(s.state_path / "mode-requests")

    def _req(self, project: str):
        return self.dir / f"{project}.json"

    def get(self, project: str) -> dict:
        pdir = self.projects.find(project)
        out = {"project": project, "mode": self.projects.mode(pdir)}
        p = self._req(project)
        if p.is_file():
            out["pending_request"] = json.loads(p.read_text())["to"]
        return out

    def set(self, project: str, mode: str) -> dict:
        mode = norm_mode(mode)
        if mode is None:
            raise ProjectError("mode must be CLOUD or LOCAL")
        pdir = self.projects.find(project)
        cur = self.projects.mode(pdir)
        rp = self._req(project)
        if mode == cur:
            rp.unlink(missing_ok=True)
            return {"project": project, "mode": cur, "changed": False}
        if mode == LOCAL:                                   # always allowed: it only restricts
            if rp.is_file():
                self.channel.close(json.loads(rp.read_text())["approval"])
                rp.unlink()
            return {**self.projects.set_mode_now(project, LOCAL), "changed": True}
        # LOCAL -> CLOUD
        if rp.is_file():
            req = json.loads(rp.read_text())
            st = self.channel.state(req["approval"])
            if req["expires"] <= time.time() or st in ("rejected", "gone"):
                self.channel.close(req["approval"])
                rp.unlink()
                return {"project": project, "mode": cur, "changed": False,
                        "state": "rejected" if st == "rejected" else "expired",
                        "next": "Not approved. The project stays LOCAL; ask again only if the user wants it."}
            if st != "approved":
                return {"project": project, "mode": cur, "changed": False, "state": "waiting_approval",
                        "next": "Waiting for the user's /pushok on the approval channel. Never ask for the code in chat."}
            self.channel.close(req["approval"])
            rp.unlink()
            return {**self.projects.set_mode_now(project, CLOUD), "changed": True, "state": "approved"}

        def message(code: str) -> str:
            return (f"🔓 Project {project}: change MODE LOCAL -> CLOUD, requested by {self.s.consumer}.\n"
                    f"CLOUD means cloud models may read this project's files, logs and configs.\n\n"
                    f"Approve: /pushok {code}\nReject:  /pushno {code}\nExpires in {self.ttl // 60} min.")

        try:
            hashes = self.channel.request(f"NAVIGATOR mode {project}", message)
        except approval.ApprovalError as e:
            raise ProjectError(str(e)) from None
        fd = os.open(rp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"to": CLOUD, "approval": hashes, "expires": time.time() + self.ttl}).encode())
        finally:
            os.close(fd)
        return {"project": project, "mode": cur, "changed": False, "state": "waiting_approval",
                "next": "Approval request sent to the user's approval channel. After their /pushok, call "
                        "mode_set again. Never ask for the code in chat."}
