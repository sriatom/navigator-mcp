"""The mechanical QC check for a chat plugin (navigator-qc): stdin {"item", "role"} -> stdout JSON.

{"verdict": "REJECT" | null, "evidence_cited": bool, "reply": "..."}  - reply only for a REJECT, ready to post.
No model, no device, no project: it only looks at whether the claim cites any evidence (roles.qc_check).
"""

from __future__ import annotations

import json
import sys

from . import roles


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    r = roles.qc_check(str(data.get("role") or "user"), str(data.get("item") or ""))
    out = {"verdict": r.get("verdict"), "evidence_cited": r["evidence_cited"]}
    if r.get("verdict") == "REJECT":
        out["reply"] = roles.REJECT_NO_EVIDENCE + "\n\n" + roles.RESEND
    print(json.dumps(out))


if __name__ == "__main__":
    main()
