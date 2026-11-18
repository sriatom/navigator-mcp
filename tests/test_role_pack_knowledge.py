"""Vendor-pack knowledge reaches the role briefs: a new pack teaches every role without changing the roles."""
import navigator_packs
from navigator_engineer import roles


def _pack(root, name, knowledge, commands=None):
    d = root / name
    d.mkdir()
    (d / "pack.yml").write_text(f"pack: {name}\nvendor: {name.upper()}\nplatforms: {{}}\n")
    (d / "knowledge.md").write_text(knowledge)
    if commands:
        (d / "commands.yml").write_text(commands)


KNOW = """# Acme OS: notes for NAVIGATOR's roles

Intro for everyone.

## Reading
Use acme-api.

## Changing (CONFIG_GENERATOR, CONFIG_PUSH)
Commit with acme-commit.

## Verify commands (STATE_VALIDATOR, CONFIG_PUSH)
show acme state
"""


def test_sections_go_to_the_roles_they_name(tmp_path, monkeypatch):
    _pack(tmp_path, "acme", KNOW)
    monkeypatch.setattr(navigator_packs, "HERE", tmp_path)
    monkeypatch.delenv("NAVIGATOR_PACKS", raising=False)
    sv = navigator_packs.role_knowledge("STATE_VALIDATOR")[0]["text"]
    dc = navigator_packs.role_knowledge("DATA_COLLECT")[0]["text"]
    assert "show acme state" in sv and "Use acme-api" in sv and "Intro for everyone" in sv
    assert "acme-commit" not in sv
    assert "Use acme-api" in dc and "show acme state" not in dc and "acme-commit" not in dc
    assert "acme-commit" in navigator_packs.role_knowledge("config_push")[0]["text"]


def test_a_new_pack_reaches_the_brief(tmp_path, monkeypatch):
    _pack(tmp_path, "acme", KNOW, "ACMEOS:\n  bgp:\n  - show acme bgp\n")
    monkeypatch.setattr(navigator_packs, "HERE", tmp_path)
    monkeypatch.delenv("NAVIGATOR_PACKS", raising=False)
    notes = roles.vendor_notes("STATE_VALIDATOR")
    assert "### ACME (pack `acme`)" in notes and "show acme state" in notes
    assert navigator_packs.commands()["ACMEOS"]["bgp"] == ["show acme bgp"]


def test_a_disabled_pack_teaches_nothing(tmp_path, monkeypatch):
    _pack(tmp_path, "acme", KNOW)
    _pack(tmp_path, "other", "## Reading\nother notes\n")
    monkeypatch.setattr(navigator_packs, "HERE", tmp_path)
    monkeypatch.setenv("NAVIGATOR_PACKS", "other")
    assert [n["pack"] for n in navigator_packs.role_knowledge("STATE_VALIDATOR")] == ["other"]


def test_shipped_packs_reach_state_validator():
    text = roles.vendor_notes("STATE_VALIDATOR")
    for vendor in ("ARISTA", "CISCO", "PALOALTO"):
        assert vendor in text.upper()
