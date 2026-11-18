"""Credential encryption (SPEC_ENCRYPTION.md section 9) and per-consumer filtering."""
from __future__ import annotations

import logging
import os

import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from atlas_inventory import config, vault as v
from atlas_inventory.server import mcp
from conftest import SECRET, write_boot


def cfg():
    return config.load()


def key_path(tmp_path):
    return tmp_path / "keys" / "vault.key"


def creds_text(data):
    return (data / "CREDENTIALS.yml").read_text()


def edit_creds(data, fn):
    p = data / "CREDENTIALS.yml"
    raw = yaml.safe_load(p.read_text())
    fn({e["TAG"]: e for e in raw["DATA"]})
    p.write_text(yaml.safe_dump(raw, sort_keys=False))


async def call(tool, **args) -> str:
    async with Client(mcp) as c:
        r = await c.call_tool(tool, args)
        return "\n".join(getattr(x, "text", "") for x in r.content)


# --------------------------------------------------------------------------- key creation
def test_init_creates_key_once(vault, tmp_path):
    made = v.create_key(cfg())
    kp = key_path(tmp_path)
    assert made == [kp]
    assert kp.stat().st_mode & 0o777 == 0o600
    assert kp.parent.stat().st_mode & 0o777 == 0o700
    first = kp.read_bytes()
    assert len(first) == 32
    with pytest.raises(v.VaultKeyError, match="never overwritten"):
        v.create_key(cfg())
    assert kp.read_bytes() == first


def test_first_encrypt_creates_key_then_reuses_it(vault, tmp_path):
    assert not key_path(tmp_path).exists()
    n, backup = v.encrypt_file(cfg())
    assert n == 4 and backup.exists()          # 2 usernames + 2 passwords; the blank enable stays blank
    first = key_path(tmp_path).read_bytes()
    edit_creds(vault, lambda d: d["FAB"].update(ENABLE_PASSWORD="later-added"))
    assert v.encrypt_file(cfg())[0] == 1
    assert key_path(tmp_path).read_bytes() == first


# --------------------------------------------------------------------------- round trip and format
def test_round_trip(vault):
    v.encrypt_file(cfg())
    text = creds_text(vault)
    assert SECRET not in text and "admin" not in text and "USERNAME: labuser" not in text
    assert "TAG: FAB" in text and "PASSWORD: ENC:v1:" in text   # tags and field names stay readable
    assert v.get_credential(cfg(), "FAB") == {"USERNAME": "labuser", "PASSWORD": SECRET}
    assert v.get_credential(cfg(), "LAB_PANOS") == {"USERNAME": "admin", "PASSWORD": SECRET}
    assert all(ok for _, ok, _ in v.check(cfg()))


def test_metadata_fields_stay_readable_with_their_types(vault):
    edit_creds(vault, lambda d: d["FAB"].update(CACHE_TIMEOUT=300, DESCRIPTION="lab fabric",
                                                 IS_ENABLED=True, AUTH_PASS="mail-" + SECRET))
    v.encrypt_file(cfg())
    fab = {e["TAG"]: e for e in yaml.safe_load(creds_text(vault))["DATA"]}["FAB"]
    assert fab["CACHE_TIMEOUT"] == 300 and fab["DESCRIPTION"] == "lab fabric" and fab["IS_ENABLED"] is True
    assert v.is_encrypted(fab["AUTH_PASS"]) and v.is_encrypted(fab["USERNAME"])
    assert v.get_credential(cfg(), "FAB")["AUTH_PASS"] == "mail-" + SECRET


def test_each_value_has_its_own_nonce(vault):
    v.encrypt_file(cfg())
    raw = yaml.safe_load(creds_text(vault))
    pw = [e["PASSWORD"] for e in raw["DATA"]]
    assert pw[0] != pw[1]                       # same secret, different ciphertext


def test_credential_repr_never_shows_values(vault):
    v.encrypt_file(cfg())
    c = v.get_credential(cfg(), "FAB")
    assert SECRET not in repr(c) and SECRET not in str(c) and "labuser" not in repr(c)


# --------------------------------------------------------------------------- tampering and keys
def test_value_swapped_between_tags_fails(vault):
    v.encrypt_file(cfg())
    edit_creds(vault, lambda d: d["FAB"].update(PASSWORD=d["LAB_PANOS"]["PASSWORD"]))
    with pytest.raises(v.VaultDecryptError, match="FAB.PASSWORD"):
        v.get_credential(cfg(), "FAB")
    assert v.get_credential(cfg(), "LAB_PANOS")["PASSWORD"] == SECRET


def test_value_swapped_between_fields_fails(vault):
    v.encrypt_file(cfg())
    edit_creds(vault, lambda d: d["FAB"].update(USERNAME=d["FAB"]["PASSWORD"]))
    with pytest.raises(v.VaultDecryptError, match="FAB.USERNAME"):
        v.get_credential(cfg(), "FAB")


def test_wrong_key_fails_loudly(vault, tmp_path):
    v.encrypt_file(cfg())
    kp = key_path(tmp_path)
    kp.chmod(0o600)
    kp.write_bytes(os.urandom(32))
    with pytest.raises(v.VaultDecryptError) as e:
        v.get_credential(cfg(), "FAB")
    assert SECRET not in str(e.value)
    assert not all(ok for _, ok, _ in v.check(cfg()))


def test_missing_key_with_encrypted_data_never_makes_a_new_key(vault, tmp_path):
    v.encrypt_file(cfg())
    key_path(tmp_path).unlink()
    with pytest.raises(v.VaultKeyError, match="key missing"):
        v.get_credential(cfg(), "FAB")
    with pytest.raises(v.VaultKeyError, match="holds encrypted values"):
        v.create_key(cfg())
    edit_creds(vault, lambda d: d["FAB"].update(ENABLE_PASSWORD="new-plain"))
    with pytest.raises(v.VaultKeyError):
        v.encrypt_file(cfg())
    assert not key_path(tmp_path).exists()


def test_key_file_too_open_or_wrong_size(vault, tmp_path):
    v.create_key(cfg())
    kp = key_path(tmp_path)
    kp.chmod(0o644)
    with pytest.raises(v.VaultKeyError, match="must be 600"):
        v.load_key(cfg())
    kp.chmod(0o600)
    kp.write_bytes(b"x" * 31)
    with pytest.raises(v.VaultKeyError, match="31 bytes"):
        v.load_key(cfg())


@pytest.mark.parametrize("where", ["data", "onedrive"])
def test_key_path_refused_in_data_folder_or_onedrive(vault, tmp_path, where):
    bad = vault / "vault.key" if where == "data" else tmp_path / "OneDrive" / "vault.key"
    (tmp_path / "bootstrap.yml").write_text(
        (tmp_path / "bootstrap.yml").read_text().replace(str(key_path(tmp_path)), str(bad)))
    with pytest.raises(v.VaultKeyError, match="OneDrive" if where == "onedrive" else "DATA_PATH"):
        v.create_key(cfg())
    assert not bad.exists()


# --------------------------------------------------------------------------- split key
def test_split_key(vault, tmp_path):
    part2 = tmp_path / "elsewhere" / "part2.key"
    write_boot(tmp_path, f"KEY_FILE_2: {part2}\n")
    made = v.create_key(cfg())
    assert made == [key_path(tmp_path), part2]
    assert key_path(tmp_path).read_bytes() != part2.read_bytes()
    v.encrypt_file(cfg())
    assert v.get_credential(cfg(), "FAB")["PASSWORD"] == SECRET
    saved = part2.read_bytes()
    part2.unlink()
    with pytest.raises(v.VaultKeyError, match="key missing"):
        v.get_credential(cfg(), "FAB")
    with pytest.raises(v.VaultKeyError, match="key part missing"):
        v.create_key(cfg())
    assert not part2.exists()
    v._write_new(part2, saved)
    assert v.get_credential(cfg(), "FAB")["PASSWORD"] == SECRET


# --------------------------------------------------------------------------- rekey
def test_rekey(vault, tmp_path):
    v.encrypt_file(cfg())
    old_key = v.load_key(cfg())
    before = creds_text(vault)
    r = v.rekey(cfg())
    assert r["backup"].read_text() == before
    assert r["old_keys"][0].read_bytes() == old_key
    assert v.load_key(cfg()) != old_key
    assert v.get_credential(cfg(), "FAB") == {"USERNAME": "labuser", "PASSWORD": SECRET}
    assert v.get_credential(cfg(), "LAB_PANOS")["PASSWORD"] == SECRET
    token = yaml.safe_load(creds_text(vault))["DATA"][1]["PASSWORD"]
    with pytest.raises(v.VaultDecryptError):
        v.decrypt_value(old_key, "FAB", "PASSWORD", token)
    assert not list(tmp_path.glob("keys/*.new"))


def test_rekey_refuses_when_an_earlier_one_did_not_finish(vault, tmp_path):
    v.encrypt_file(cfg())
    before = creds_text(vault)
    v._write_new(tmp_path / "keys" / "vault.key.new", os.urandom(32))
    with pytest.raises(v.VaultKeyError, match="did not finish"):
        v.rekey(cfg())
    assert creds_text(vault) == before
    assert v.get_credential(cfg(), "FAB")["PASSWORD"] == SECRET


# --------------------------------------------------------------------------- mixed file, disabled
def test_mixed_file_and_status(vault):
    v.encrypt_file(cfg())
    edit_creds(vault, lambda d: d["FAB"].update(ENABLE_PASSWORD="plain-enable"))
    assert v.get_credential(cfg(), "FAB")["ENABLE_PASSWORD"] == "plain-enable"
    assert v.counts(vault / "CREDENTIALS.yml") == {"LAB_PANOS": (2, 0), "FAB": (2, 1)}
    text = "\n".join(v.status_lines(cfg()))
    assert "FAB: 2 encrypted, 1 plain" in text and "plain-enable" not in text


def test_disabled_and_unknown_credentials_refused(vault):
    edit_creds(vault, lambda d: d["FAB"].update(IS_ENABLED=False))
    with pytest.raises(v.VaultError, match="disabled"):
        v.get_credential(cfg(), "FAB")
    with pytest.raises(v.VaultError, match="not found"):
        v.get_credential(cfg(), "NOPE")


# --------------------------------------------------------------------------- no leaks anywhere
async def test_no_secret_in_logs_cli_or_tool_output(vault, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    rc = [v.main(["init"]), v.main(["encrypt"]), v.main(["status"]), v.main(["check"]),
          v.main(["rekey"]), v.main(["check"])]
    assert rc == [0, 0, 0, 0, 0, 0]
    v.get_credential(cfg(), "FAB")
    v.get_credential(cfg(), "LAB_PANOS")
    outs = [await call("inventory_list"), await call("credential_list"),
            await call("inventory_show", name="FAB"), await call("inventory_validate", name="PALAB")]
    cap = capsys.readouterr()
    blob = "\n".join([caplog.text, cap.out, cap.err, *outs])
    assert SECRET not in blob
    assert "admin" not in blob and "labuser" not in blob
    assert "credential served tag=FAB" in caplog.text
    assert "stored encrypted" in outs[1]


def test_cli_reports_errors_without_traceback(vault, tmp_path, capsys):
    v.main(["encrypt"])
    key_path(tmp_path).unlink()
    assert v.main(["check"]) == 1
    assert "ERROR: key missing" in capsys.readouterr().err


# --------------------------------------------------------------------------- consumers
CONSUMERS = """\
CONSUMERS:
  NAVI:    {INVENTORIES: ["*"], CREDENTIALS: ["*"]}
  LAB_BOT: {INVENTORIES: [INVENTORY_PALAB], CREDENTIALS: [LAB_PANOS]}
"""


async def test_consumer_lab_bot_sees_only_its_share(vault, tmp_path, monkeypatch):
    write_boot(tmp_path, CONSUMERS)
    monkeypatch.setenv("NAVI_CONSUMER", "lab_bot")
    out = await call("inventory_list")
    assert "INVENTORY_PALAB" in out and "INVENTORY_FAB" not in out
    with pytest.raises(ToolError, match="not available to consumer LAB_BOT"):
        await call("inventory_show", name="FAB")
    with pytest.raises(ToolError) as e:
        await call("inventory_show", name="NOPE")
    assert "INVENTORY_FAB" not in str(e.value)
    show = await call("inventory_show", name="PALAB")
    assert "cred=LAB_PANOS" in show and "cred=(not allowed)" in show and "cred=LAB_EOS" not in show
    creds = await call("credential_list")
    assert "LAB_PANOS" in creds and "FAB" not in creds and "LAB_EOS" not in creds
    v.encrypt_file(cfg())
    assert v.get_credential(cfg(), "LAB_PANOS")["PASSWORD"] == SECRET
    with pytest.raises(PermissionError, match="LAB_BOT may not use credential FAB"):
        v.get_credential(cfg(), "FAB")
    assert v.get_credential(cfg(), "FAB", consumer="NAVI")["PASSWORD"] == SECRET


async def test_unlisted_consumer_gets_nothing(vault, tmp_path, monkeypatch):
    write_boot(tmp_path, CONSUMERS)
    monkeypatch.setenv("NAVI_CONSUMER", "STRANGER")
    assert await call("inventory_list") == "No inventories found."
    assert await call("credential_list") == "No credentials stored."
    with pytest.raises(PermissionError):
        v.get_credential(cfg(), "LAB_PANOS")


def test_bad_consumers_section_is_rejected(vault, tmp_path):
    write_boot(tmp_path, "CONSUMERS:\n  LAB_BOT: {INVENTORIES: INVENTORY_PALAB}\n")
    with pytest.raises(ValueError, match="must be a list"):
        cfg()
