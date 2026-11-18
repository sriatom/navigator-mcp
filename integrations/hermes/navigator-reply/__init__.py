"""navigator-reply: post change results back in the chat thread the change came from.

pre_tool_call: when this profile's agent calls the navigator `change` tool with action=propose, set
  reply_to     "<profile>/<platform>:<chat_id>[:<thread_id>]"  (RUDDER posts the result there), and
  requested_by "<user_id>|<user_name>"                         (who asked: ticket search, SELF_APPROVE,
                                                                 SHIP_ENGINEERS)
from the session's own origin (Hermes state.db). The model never fills them: whatever it put there is
replaced, or removed when the origin is unknown (so it cannot claim to be a SHIP engineer).
Approval still happens only on the approval bot.

transform_llm_output: only RUDDER reports a change result. RUDDER's results are mirrored into the session
  as assistant messages, so the model learns the pattern and starts typing "✅ Change C...: applied" itself,
  before anyone approved (2026-11-15: two fake results plus RUDDER's real one in one thread). Any such
  RUDDER-style status block in the model's own reply is removed: the chat gets exactly ONE result per
  ticket, the one RUDDER sends.

pre_gateway_dispatch: a message that starts or opens a LOCAL project ("new LOCAL project X", "project X
  as local"; the older word LOCKED too) on a cloud model switches its thread to the local model first, exactly as if the user
  had typed `/model local` (the profile's model_aliases entry). Only authorised senders; /model local and
  /model cloud keep working by hand, in any case. NAVIGATOR protects LOCKED data on the host either way (#152): this makes the
  chat itself run locally too.

Thread project: the project a thread works in (from `project create`, or any successful navigator call
  that named one) is remembered per session, and filled in as `project=` on later navigator calls that leave
  it out. Small local models forget it, and without it a LOCAL project's scope (its inventory, its records)
  is not applied. An explicit project= is never replaced.

host_model: on navigator project / device / troubleshoot / local_model calls, host_model is set to the
  thread's real model ("local" or "cloud", from its last API request), whatever the model wrote; NAVIGATOR
  trusts it because this profile's MCP config sets NAVIGATOR_TRUST_HOST_MODEL=1. A new project takes it as
  its mode (a project follows the model it was started on, #156); a LOCAL project on the local model gets
  its device output back.

post_tool_call: after navigator `project create` the thread gets a card: project, folder, CLOUD/LOCAL,
  inventory, outside ticket, and the model the thread runs on (local or cloud), with a warning when a LOCKED
  project is on a cloud model.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sqlite3
from urllib.parse import urlparse

log = logging.getLogger("navigator-reply")
CHANGE_TOOLS = ("mcp__navigator__change", "mcp_navigator_change", "navigator__change")
PLATFORMS = ("slack", "telegram", "discord")
# The first line of every message RUDDER itself sends (schedule.py, changes.py): result, missed, stopped,
# notice, approval request, SHIP bypass report. The model must never write one.
RUDDER_HEAD = re.compile(r"^\s*(?:✅|❌|⏭|🛑|⏰|🔐|ℹ|⚠)️?\s*(?:Scheduled change|Change)\s+C\d{8}-[0-9a-f]{6}\b")
_KEEP = re.compile(r"^\s*(?:[•*>#|-]|```|\d+[.)]\s)")     # model text (bullets, headings, code) ends a block
PROJECT_TOOLS = ("mcp__navigator__project", "mcp_navigator_project", "navigator__project")
LOCAL_ALIAS = os.environ.get("NAVIGATOR_LOCAL_ALIAS", "local")
# "new LOCKED project X", "locked project", "project X ... as/in/mode LOCKED". Case-insensitive.
LOCKED_INTENT = re.compile(r"\b(?:locked|local)\s+(?:project|job|mode)\b"
                           r"|\bproject\b[^\n]{0,80}?\b(?:as|in|to|mode|is|on)\s*:?\s*\*?(?:locked|local)\b",
                           re.IGNORECASE)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "192.0.2.240"}
_models: dict[str, dict] = {}       # session_id -> {"model", "base_url"} of its last main request
_switched: dict[str, str] = {}      # gateway session key -> what the auto-switch did
_ctx = None
NAV_PREFIXES = ("mcp__navigator__", "mcp_navigator_", "navigator__")
# navigator tools that take project= (project and mode name it themselves; skill takes none)
PROJECT_SCOPED = {"inventory", "inventory_edit", "credential", "device", "adapter", "change", "troubleshoot",
                  "scrambler", "research", "local_model", "role", "qc", "lesson"}
_projects: dict[str, str] | None = None     # session_id -> the thread's project (file-backed)
HOST_TOOLS = {"project", "device", "troubleshoot", "local_model"}      # navigator tools that take host_model


def _nav(name) -> str | None:
    for p in NAV_PREFIXES:
        if isinstance(name, str) and name.startswith(p):
            return name[len(p):]
    return None


def _proj_file() -> str:
    return os.path.join(_home(), "navigator-reply-projects.json")


def _load_projects() -> dict[str, str]:
    global _projects
    if _projects is None:
        try:
            with open(_proj_file(), encoding="utf-8") as f:
                _projects = {str(k): str(v) for k, v in json.load(f).items()}
        except Exception:  # noqa: BLE001
            _projects = {}
    return _projects


def thread_project(session_id: str) -> str | None:
    return _load_projects().get(session_id) if session_id else None


def remember_project(session_id: str, name) -> None:
    if not session_id or not isinstance(name, str) or not name.strip():
        return
    p = _load_projects()
    if p.get(session_id) == name:
        return
    p.pop(session_id, None)
    p[session_id] = name
    for k in list(p)[:-500]:
        del p[k]
    try:
        tmp = _proj_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f)
        os.replace(tmp, _proj_file())
    except Exception as e:  # noqa: BLE001
        log.warning("navigator-reply: thread project not saved: %s", e)


def _fill_project(name, args, session_id: str) -> dict:
    """{"project": <thread project>} for a project-scoped navigator call that has none; else {}."""
    if _nav(name) not in PROJECT_SCOPED or not isinstance(args, dict) or args.get("project"):
        return {}
    proj = thread_project(session_id)
    return {"project": proj} if proj else {}


def strip_fake_results(text: str) -> str | None:
    """The reply without RUDDER-style status blocks (header + its plain lines up to a blank line);
    None when there were none."""
    if not isinstance(text, str) or not any(RUDDER_HEAD.match(ln) for ln in text.splitlines()):
        return None
    out, skipping, removed = [], False, 0
    for ln in text.splitlines():
        if RUDDER_HEAD.match(ln):
            skipping, removed = True, removed + 1
            continue
        if skipping:
            if not ln.strip() or _KEEP.match(ln):
                skipping = False
            else:
                continue
        out.append(ln)
    if not removed:
        return None
    clean = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return clean or "RUDDER posts the result here once the change is approved and applied."


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # noqa: BLE001
        return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _origin_args(session_id: str) -> dict:
    """{"reply_to": ..., "requested_by": "ID|Name"} from the session's origin; {} if unknown."""
    o = _origin(session_id)
    if not o:
        return {}
    out = {}
    t = _target_from(o)
    if t:
        out["reply_to"] = t
    uid = str(o.get("user_id") or "").strip()
    uname = str(o.get("user_name") or "").strip()
    if uid or uname:
        out["requested_by"] = f"{uid}|{uname}" if uid else uname
    return out


def _origin(session_id: str) -> dict:
    if not session_id:
        return {}
    home = _home()
    try:
        con = sqlite3.connect(f"file:{os.path.join(home, 'state.db')}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select origin_json from sessions where id=?", (session_id,)).fetchone()
        finally:
            con.close()
        o = json.loads(row[0]) if row and row[0] else {}
    except Exception as e:  # noqa: BLE001
        log.debug("navigator-reply: no origin for %s: %s", session_id, e)
        return {}
    o.setdefault("profile", os.path.basename(home.rstrip("/")))
    return o


def _target_from(o: dict) -> str | None:
    platform, chat = str(o.get("platform") or ""), str(o.get("chat_id") or "")
    if platform not in PLATFORMS or not chat:
        return None
    thread = str(o.get("thread_id") or "")
    return f"{o['profile']}/{platform}:{chat}" + (f":{thread}" if thread else "")


def _is_propose(name: str, args) -> bool:
    return isinstance(args, dict) and name in CHANGE_TOOLS and str(args.get("action", "")).lower() == "propose"


def _host_args(session_id: str) -> dict:
    """What the host fills; the model's own reply_to / requested_by are always replaced (or removed)."""
    got = _origin_args(session_id)
    return {"reply_to": got.get("reply_to"), "requested_by": got.get("requested_by")}


def host_model(session_id: str) -> str | None:
    """local / cloud: the thread's real model (its last API request), None when not known yet."""
    m = _models.get(session_id) or {}
    if not m.get("base_url"):
        return None
    return "local" if _is_local(m["base_url"]) else "cloud"


def _extra(name, args, sid: str) -> dict:
    extra = _host_args(sid) if _is_propose(name, args) else {}
    extra.update(_fill_project(name, args, sid))
    if _nav(name) in HOST_TOOLS and isinstance(args, dict) and (args.get("host_model") != host_model(sid)):
        extra["host_model"] = host_model(sid)       # never the model's own claim
    return extra


def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    args = args if isinstance(args, dict) else {}
    sid = kw.get("session_id") or ""
    calls = args.get("calls")                    # the tool_call wrapper: [{"name", "arguments"}]
    if tool_name == "tool_call" and isinstance(calls, list):
        new, changed = [], False
        for c in calls:
            if isinstance(c, dict) and isinstance(c.get("arguments"), dict):
                extra = _extra(c.get("name", ""), c["arguments"], sid)
                if extra:
                    c, changed = {**c, "arguments": {**c["arguments"], **extra}}, True
            new.append(c)
        return {"action": "modify", "args": {"calls": new}} if changed else None
    extra = _extra(tool_name, args, sid)
    return {"action": "modify", "args": extra} if extra else None


def on_transform_llm_output(response_text: str = "", **kw):
    """Only RUDDER reports results: drop any result the model typed itself (first non-None wins)."""
    clean = strip_fake_results(response_text)
    if clean is not None:
        log.info("navigator-reply: removed a RUDDER-style result the model wrote itself (session %s)",
                 kw.get("session_id", ""))
    return clean


# --------------------------------------------------------------------------- LOCKED: switch the thread
def wants_locked(text) -> bool:
    """A message that starts or opens a LOCAL project (never a command, never a quoted /model)."""
    return isinstance(text, str) and not text.lstrip().startswith("/") and bool(LOCKED_INTENT.search(text))


def _is_local(url: str) -> bool:
    try:
        return (urlparse(url or "").hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


async def on_gateway_dispatch(event=None, gateway=None, **_kw):
    """Before the message runs: if it asks for a LOCAL project, run `/model local` for its thread."""
    text = getattr(event, "text", None)
    if gateway is None or not wants_locked(text):
        return None
    try:
        source = await asyncio.to_thread(gateway._normalize_source_for_session_key, event.source)
        key = gateway._session_key_for_source(source)
        auth = getattr(gateway, "_is_user_authorized", None)
        if auth is not None and not auth(source):
            return None                                   # never for a sender the gateway will refuse
        cur = (getattr(gateway, "_session_model_overrides", {}) or {}).get(key) or {}
        if _is_local(cur.get("base_url", "")):
            _switched[key] = "already"
            return None
        cmd = dataclasses.replace(event, text=f"/model {LOCAL_ALIAS}")
        reply = await gateway._handle_model_command(cmd)
        now = (getattr(gateway, "_session_model_overrides", {}) or {}).get(key) or {}
        _switched[key] = "switched" if _is_local(now.get("base_url", "")) else f"failed: {str(reply)[:200]}"
        log.info("navigator-reply: LOCAL project message -> /model %s for %s: %s",
                 LOCAL_ALIAS, key, _switched[key])
    except Exception as e:  # noqa: BLE001 - never stop the message; the card says what happened
        _switched["?"] = f"failed: {type(e).__name__}"
        log.warning("navigator-reply: auto /model %s failed: %s", LOCAL_ALIAS, e)
    return None


def on_api_request(**kw) -> None:
    sid = kw.get("session_id") or ""
    if sid:
        _models[sid] = {"model": kw.get("model") or "", "base_url": kw.get("base_url") or ""}


# --------------------------------------------------------------------------- the project card
def _result_dict(result):
    """The tool's JSON object, unwrapped from Hermes/MCP wrappers ({"result": "<json>"}, text blocks)."""
    for _ in range(4):
        if isinstance(result, dict):
            if "path" in result and "mode" in result:
                return result
            inner = result.get("result") or result.get("structuredContent") or result.get("content")
            if inner is None:
                return None
            result = inner
        elif isinstance(result, list) and result:
            result = result[0].get("text") if isinstance(result[0], dict) else result[0]
        elif isinstance(result, str):
            i, j = result.find("{"), result.rfind("}")
            if i < 0 or j <= i:
                return None
            try:
                result = json.loads(result[i:j + 1])
            except ValueError:
                return None
        else:
            return None
    return None


def _is_error(result) -> bool:
    """True when a tool result (possibly wrapped) is an error, or cannot be read."""
    for _ in range(4):
        if isinstance(result, dict):
            if result.get("error") or result.get("isError"):
                return True
            inner = result.get("structuredContent") or result.get("content")
            if inner is None and isinstance(result.get("result"), (str, list)):
                inner = result["result"]
            if inner is None:
                return False
            result = inner
        elif isinstance(result, list) and result:
            result = result[0].get("text") if isinstance(result[0], dict) else result[0]
        elif isinstance(result, str):
            i, j = result.find("{"), result.rfind("}")
            if i < 0 or j <= i:
                return True
            try:
                result = json.loads(result[i:j + 1])
            except ValueError:
                return True
        else:
            return True
    return False


def card(res: dict, model: dict, switched: str | None) -> str:
    locked = str(res.get("mode", "")).upper() in ("LOCAL", "LOCKED")
    name, local = res.get("project", "?"), _is_local(model.get("base_url", ""))
    mname = model.get("model") or "unknown"
    lines = [f"{'🔒' if locked else '☁️'} *Project {name}*" + (" (new)" if res.get("created") else " (opened)"),
             f"• Folder: `{res.get('path', '?')}`",
             f"• Mode: *{'LOCAL' if locked else 'CLOUD'}*" + (f"   • Inventory: `{res['inventory']}`"
                                                             if res.get("inventory") else ""),
             f"• Model: `{mname}` ({'local' if local else 'cloud'})"
             + (" — switched automatically for LOCAL" if switched == "switched" and local else "")]
    if res.get("ticket_id"):
        lines.insert(3, f"• Ticket: `{res['ticket_id']}`")
    if locked and not local:
        lines.append("⚠️ This thread is on a cloud model. NAVIGATOR keeps the project's data on the host; "
                     "type `/model local` to run the chat on the local model too.")
    for n in res.get("notes") or []:
        lines.append(f"• Note: {n}")
    return "\n".join(lines)


def on_post_tool_call(tool_name: str = "", args=None, result=None, **kw) -> None:
    args = args if isinstance(args, dict) else {}
    short, sid = _nav(tool_name), kw.get("session_id") or ""
    if short is None:
        return None
    if _is_error(result):
        return None
    create = short == "project" and str(args.get("action", "")).lower() == "create"
    res = _result_dict(result) if create else None
    if create and res:
        remember_project(sid, res.get("project") or args.get("project"))
    elif not create and short in PROJECT_SCOPED | {"project", "mode"} and args.get("project"):
        remember_project(sid, args["project"])
    if not create or not res or _ctx is None:
        return None
    try:
        from gateway.session_context import bound_session_key
        key = bound_session_key() or ""
    except Exception:  # noqa: BLE001
        key = ""
    try:
        _ctx.post_message(card(res, _models.get(sid, {}), _switched.pop(key, None)))
    except Exception as e:  # noqa: BLE001
        log.warning("navigator-reply: project card not posted: %s", e)
    return None


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("pre_gateway_dispatch", on_gateway_dispatch)
    ctx.register_hook("pre_api_request", on_api_request)
    ctx.register_hook("post_tool_call", on_post_tool_call)
