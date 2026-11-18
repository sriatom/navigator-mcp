"""navigator-qc: QC requests that do not depend on the model (2026-11-18).

A small local model asked "QC this: QALEAF1A has 12 EVPN peers" went and read the switch 18 times, never
called qc, and answered "QC VERDICT: PASS WITH NOTES". QC judges the evidence that comes WITH a claim, and a
claim with no evidence is REJECT: that needs no model at all.

pre_gateway_dispatch, for a message that starts with "QC this", "QC:", "please QC", "sanity-check this"...:
  - the claim cites no evidence (NAVIGATOR's own check: no record or file path, URL, line N, code block),
    is short, and does not point back at the thread ("the last result", "your answer", "above"):
    the message becomes the plugin command /navqc <token>, whose reply is NAVIGATOR's REJECT text. The model
    never sees it, no device is read.
  - anything else (evidence given, a reference to the thread, or the check failed): the message goes to the
    model as usual, with one line in front telling it to call qc first, judge only the evidence given and
    treat unverifiable or contradicted as REJECT.
The rewrite runs before the gateway's own checks, so an unauthorised sender is still refused as usual.
Only the standard library: the check itself runs in NAVIGATOR's venv (python -m navigator_engineer.qc_cli).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time

log = logging.getLogger("navigator-qc")
PYTHON = os.environ.get("NAVIGATOR_PYTHON", os.path.expanduser("~/navigator/.venv/bin/python"))
CMD = "navqc"
KEEP_SEC = 600

_ASK = re.compile(r"^[\s`*_~>]*(?:<@[A-Za-z0-9]+>[\s,:]*)?(?:please\s+|pls\s+|can you\s+|could you\s+)?"
                  r"(?:qc|sanity[- ]check)\b[\s:,\-`*_~]*(?:(?:this|the following|my)\b[\s`*_~]*)?"
                  r"(?:(?:claim|statement)\b[\s`*_~]*)?[:,\-`*_~\s]*", re.I)
# A claim about something earlier in the thread needs the model (it has the thread).
_REFERS = re.compile(r"(?i)\b(above|last|previous|earlier|prior|your|thread|these|those|result|answer|output|"
                     r"report|plan|summary)\b")
GUIDE = ("[NAVIGATOR QC] Call qc action=check first (role = who produced it, item = the claim with its evidence). "
         "Judge only the evidence that comes with the claim; do not read devices or inventories to check it. "
         "Evidence that is missing, does not show the claim, or contradicts it = REJECT; \"could not verify\" is "
         "REJECT. PASS WITH NOTES only when the evidence supports the claim. Start the reply with "
         "'QC VERDICT: PASS | PASS WITH NOTES | REJECT'.\n\n")

_pending: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def qc_request(text) -> str | None:
    """The claim of a QC request, or None when the message is not one."""
    if not isinstance(text, str) or text.lstrip().startswith("/") or text.startswith("[NAVIGATOR QC]"):
        return None
    m = _ASK.match(text)
    if not m:
        return None
    claim = text[m.end():].strip()
    return claim or None


def model_free(claim: str) -> bool:
    """Short, standalone claims only: pasted output (several lines) or a reference to the thread go to the model."""
    lines = [ln for ln in claim.splitlines() if ln.strip()]
    return len(lines) <= 2 and len(claim) <= 400 and not _REFERS.search(claim)


def check(claim: str) -> dict | None:
    try:
        p = subprocess.run([PYTHON, "-m", "navigator_engineer.qc_cli"], input=json.dumps({"item": claim}),
                           capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout)
    except Exception as exc:  # noqa: BLE001 - fall back to the model path
        log.warning("navigator-qc: check failed: %s", exc)
        return None


def _keep(reply: str) -> str:
    token = secrets.token_hex(4)
    now = time.time()
    with _lock:
        for k in [k for k, (t, _) in _pending.items() if now - t > KEEP_SEC]:
            _pending.pop(k, None)
        _pending[token] = (now, reply)
    return token


def on_gateway_dispatch(event=None, **_kw):
    text = getattr(event, "text", None)
    claim = qc_request(text)
    if claim is None:
        return None
    if model_free(claim):
        r = check(claim)
        if r and r.get("verdict") == "REJECT" and r.get("reply"):
            log.info("navigator-qc: no evidence with the claim -> REJECT without the model")
            return {"action": "rewrite", "text": f"/{CMD} {_keep(r['reply'])}"}
    log.info("navigator-qc: QC request goes to the model with the QC guide line")
    return {"action": "rewrite", "text": GUIDE + text}


def navqc(raw: str = "") -> str:
    with _lock:
        hit = _pending.pop(str(raw or "").strip(), None)
    return hit[1] if hit else "QC: that request has expired. Send it again, starting with \"QC this:\"."


def register(ctx) -> None:
    ctx.register_command(CMD, navqc, description="NAVIGATOR QC reply (used by the navigator-qc plugin)",
                         args_hint="<token>")
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
