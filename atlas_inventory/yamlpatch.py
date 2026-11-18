"""Minimal text edits to hand-written YAML inventories, so a change touches only its own lines.

Re-dumping a whole file (PyYAML or ruamel) reformats hundreds of lines of a hand-kept inventory.
These helpers instead:
  - append_records: add records at the end of a block list, in the file's own item style;
  - edit_record: rewrite only the changed keys of one record (new values in one-line flow style),
    delete removed keys, add new keys under the record.
Every result is re-parsed and must equal the intended document; otherwise None is returned and the
caller falls back to a full rewrite (and says so in the preview).
"""
from __future__ import annotations

import yaml


def flow(v) -> str:
    s = yaml.safe_dump(v, default_flow_style=True, width=10**6, allow_unicode=True, sort_keys=False).strip()
    return s[:-3].strip() if s.endswith("...") else s       # scalars end with a document marker


def _seq(text: str, key: str):
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    for k, v in root.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            if isinstance(v, yaml.SequenceNode) and not v.flow_style and v.value:
                return v
            return None
    return None


def _last_end(node) -> int:
    """End of the last scalar inside a node (block collection end marks can run past comments)."""
    if isinstance(node, yaml.MappingNode) and not node.flow_style and node.value:
        return _last_end(node.value[-1][1])
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        return _last_end(node.value[-1])
    return node.end_mark.index


def _line_start(text: str, idx: int) -> int:
    return text.rfind("\n", 0, idx) + 1


def _content_end(text: str, idx: int) -> int:
    """Index just after the last non-blank character before idx."""
    while idx > 0 and text[idx - 1] in " \t\r\n":
        idx -= 1
    return idx


def _after_line(text: str, idx: int) -> int:
    """Index of the start of the line after the one holding idx-1."""
    nl = text.find("\n", idx)
    return len(text) if nl < 0 else nl + 1


def _style(text: str, seq) -> tuple[int, int, bool]:
    """(dash column, key column, dash alone on its line) from the first item."""
    first = seq.value[0]
    if not isinstance(first, yaml.MappingNode) or first.flow_style:
        raise ValueError("items are not block mappings")
    key_idx = first.start_mark.index
    dash_idx = text.rfind("-", 0, key_idx)
    dash_col = dash_idx - _line_start(text, dash_idx)
    alone = text[dash_idx + 1:key_idx].strip() == "" and "\n" in text[dash_idx:key_idx]
    return dash_col, first.start_mark.column, alone


def _block(rec: dict, dash: int, keycol: int, alone: bool) -> str:
    items = list(rec.items())
    lines = []
    if alone or keycol != dash + 2:
        lines.append(" " * dash + "-")
        lines += [" " * keycol + f"{k}: {flow(v)}" for k, v in items]
    else:
        k0, v0 = items[0]
        lines.append(" " * dash + f"- {k0}: {flow(v0)}")
        lines += [" " * keycol + f"{k}: {flow(v)}" for k, v in items[1:]]
    return "\n".join(lines) + "\n"


def append_records(text: str, list_key: str, records: list[dict], expect: dict) -> str | None:
    seq = _seq(text, list_key)
    if seq is None or not records:
        return None
    first = seq.value[0]
    at = _after_line(text, _content_end(text, _last_end(seq)))
    head = text[:at] if text[:at].endswith("\n") or at == 0 else text[:at] + "\n"
    if isinstance(first, yaml.MappingNode) and first.flow_style:     # one-line records: `- {...}`
        dash_idx = text.rfind("-", 0, first.start_mark.index)
        dash = dash_idx - _line_start(text, dash_idx)
        body = "".join(" " * dash + f"- {flow(r)}\n" for r in records)
    else:
        try:
            dash, keycol, alone = _style(text, seq)
        except ValueError:
            return None
        body = "".join(_block(r, dash, keycol, alone) for r in records)
    out = head + body + text[at:]
    return out if _same(out, expect) else None


def edit_record(text: str, list_key: str, index: int, old: dict, new: dict, expect: dict) -> str | None:
    seq = _seq(text, list_key)
    if seq is None or index >= len(seq.value):
        return None
    node = seq.value[index]
    if not isinstance(node, yaml.MappingNode):
        return None
    if node.flow_style:
        return _edit_flow(text, node, old, new, expect)
    edits: list[tuple[int, int, str]] = []          # (start, end, replacement)
    present = {}
    for k, v in node.value:
        present[k.value] = (k, v)
    for key, (kn, vn) in present.items():
        if key not in new:
            s = _line_start(text, kn.start_mark.index)
            e = _after_line(text, _content_end(text, _last_end(vn)))
            edits.append((s, e, ""))
        elif new[key] != old.get(key):
            s = kn.start_mark.index
            e = _content_end(text, _last_end(vn))
            edits.append((s, e, f"{key}: {flow(new[key])}"))
    added = [k for k in new if k not in present]
    if added:
        last_v = node.value[-1][1]
        at = _after_line(text, _content_end(text, _last_end(last_v)))
        col = node.start_mark.column
        edits.append((at, at, "".join(" " * col + f"{k}: {flow(new[k])}\n" for k in added)))
    out = text
    for s, e, rep in sorted(edits, key=lambda x: x[0], reverse=True):
        out = out[:s] + rep + out[e:]
    return out if _same(out, expect) else None


def _edit_flow(text: str, node, old: dict, new: dict, expect: dict) -> str | None:
    """One-line records like `- {DEVICE_NAME: R1, CREDENTIAL: X, ...}`: replace only the changed
    scalar values in place; if keys are added/removed or a nested value changes, rewrite just
    this record as one flow mapping."""
    present = {k.value: (k, v) for k, v in node.value}
    simple = set(new) == set(present) and all(
        isinstance(present[k][1], yaml.ScalarNode) and not isinstance(new[k], (dict, list))
        for k in new if new[k] != old.get(k))
    if simple:
        out = text
        spans = sorted(((present[k][1].start_mark.index, present[k][1].end_mark.index, flow(new[k]))
                        for k in new if new[k] != old.get(k)), reverse=True)
        for s, e, rep in spans:
            out = out[:s] + rep + out[e:]
    else:
        s, e = node.start_mark.index, node.end_mark.index
        out = text[:s] + flow(new) + text[e:]
    return out if _same(out, expect) else None


def _same(text: str, expect: dict) -> bool:
    try:
        return yaml.safe_load(text) == expect
    except yaml.YAMLError:
        return False
