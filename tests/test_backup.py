"""The recovery key, full backup and restore (decision #148)."""
from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml

from atlas_inventory import config as aconfig
from atlas_inventory import recovery, vault
from navigator_engineer import backup


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config/navigator/tls").mkdir(parents=True)
    (home / ".config/navigator/navigator.yml").write_text(f"PROJECT_ROOT: {tmp_path}\nSTATE_PATH: ~/.navigator\n")
    (home / ".config/navigator/users.yml").write_text("USERS: {}\n")
    (home / ".config/navigator/tls/ca.pem").write_text("CA\n")
    (home / ".config/rudder").mkdir(parents=True)
    (home / ".config/rudder/atlas-link.token").write_text("TOKEN-SECRET-123\n")
    for d in ("tickets", "records", "delegated", "locks"):
        (home / ".rudder" / d).mkdir(parents=True)
    (home / ".rudder/tickets/T1.json").write_text('{"id": "T1"}')
    (home / ".rudder/records/r.txt").write_text("show version\n")
    (home / ".rudder/delegated/huge.bin").write_text("software")
    (home / ".rudder/audit.log").write_text("audit\n")
    (home / ".navigator").mkdir()
    (home / ".navigator/LESSONS_LEARNED.md").write_text("lesson\n")
    data = tmp_path / "vaultdata"
    data.mkdir()
    (data / "INVENTORY_LAB.yml").write_text("DEVICES: []\n")
    (data / "CREDENTIALS.yml").write_text(yaml.safe_dump({"DATA": [
        {"TAG": "LAB", "USERNAME": "admin", "LOGIN_PASSWORD": "Pl4in-Secret!"}]}))
    boot = tmp_path / "bootstrap.yml"
    boot.write_text(f"DATA_PATH: {data}\nKEY_FILE: ~/.config/atlas/vault.key\nSTATE_PATH: ~/.atlas\n")
    (home / ".atlas").mkdir()
    (tmp_path / "rudder.yml").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ATLAS_BOOTSTRAP", str(boot))
    monkeypatch.setenv("RUDDER_BOOTSTRAP", str(tmp_path / "rudder.yml"))
    monkeypatch.setenv("NAVIGATOR_BOOTSTRAP", str(home / ".config/navigator/navigator.yml"))
    for v in ("NAVIGATOR_CONFIG_DIR", "NAVIGATOR_TLS_DIR", "NAVIGATOR_USERS", "RUDDER_LOCAL"):
        monkeypatch.delenv(v, raising=False)
    cfg = aconfig.load()
    vault.encrypt_file(cfg)                              # makes the CREDENTIALS key, encrypts the values
    for p in data.glob("CREDENTIALS.yml.*"):
        p.unlink()                                       # the plain-text copy encrypt_file keeps
    return {"home": home, "data": data, "tmp": tmp_path, "cfg": cfg}


def test_recovery_key_format_and_typos():
    rk = recovery.new_recovery_key()
    assert re.fullmatch(r"(\d{6}-){7}\d{6}", rk)
    assert len(recovery.parse(rk)) == 16 and recovery.parse(rk.replace("-", " ")) == recovery.parse(rk)
    bad = str((int(rk[:6]) + 1) % 1000000).zfill(6) + rk[6:]
    with pytest.raises(recovery.RecoveryError, match="group 1"):
        recovery.parse(bad)
    with pytest.raises(recovery.RecoveryError, match="48 digits"):
        recovery.parse("123")


def test_init_check_and_rekey(host):
    cfg = host["cfg"]
    rk = recovery.init(cfg)
    p = recovery.path_for(cfg)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert rk.replace("-", "") not in p.read_text()
    recovery.check(cfg, rk)
    with pytest.raises(recovery.RecoveryError, match="not the recovery key"):
        recovery.check(cfg, recovery.new_recovery_key())
    with pytest.raises(recovery.RecoveryError, match="exists"):
        recovery.init(cfg)
    vault.rekey(cfg)                                     # a new CREDENTIALS key: the escrow follows by itself
    recovery.check(cfg, rk)


def test_backup_and_restore_to_a_folder(host):
    rk = recovery.init(host["cfg"])
    res = backup.create(rk, host["tmp"] / "out")
    f = Path(res["file"])
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and f.name.startswith("NAVIGATOR-BACKUP-")
    blob = f.read_bytes()
    key = vault.load_key(host["cfg"])
    for secret in (b"TOKEN-SECRET-123", key, b"Pl4in-Secret!", b"show version"):
        assert secret not in blob
    labels = {s.split(" ")[0] for s in res["sources"]}
    assert {"navigator-config", "navigator-state", "rudder-config", "rudder-state", "atlas-key",
            "atlas-data", "atlas-state"} <= labels
    info = backup.read(f, rk)
    names = set(info["files"])
    assert "rudder-state/tickets/T1.json" in names and "atlas-key/vault.key" in names
    assert "atlas-data/CREDENTIALS.yml" in names and "navigator-config/tls/ca.pem" in names
    assert not any("delegated" in n or "locks" in n for n in names)
    out = backup.restore(f, rk, to=host["tmp"] / "staged")
    st = host["tmp"] / "staged"
    assert (st / "atlas-key/vault.key").read_bytes() == key
    assert (st / "rudder-config/atlas-link.token").read_text() == "TOKEN-SECRET-123\n"
    assert out["moved_aside"] == []
    nr = backup.create(rk, host["tmp"] / "out2", records=False)
    assert not any(n.startswith("rudder-state/records") for n in backup.read(Path(nr["file"]), rk)["files"])


def test_wrong_key_damage_and_cut_files(host):
    rk = recovery.init(host["cfg"])
    with pytest.raises(recovery.RecoveryError):
        backup.create(recovery.new_recovery_key(), host["tmp"] / "out")
    assert not (host["tmp"] / "out").exists() or not list((host["tmp"] / "out").iterdir())
    f = Path(backup.create(rk, host["tmp"] / "out")["file"])
    with pytest.raises(recovery.RecoveryError):
        backup.read(f, recovery.new_recovery_key())
    raw = bytearray(f.read_bytes())
    bad = host["tmp"] / "bad.nvbak"
    raw[-20] ^= 1
    bad.write_bytes(bytes(raw))
    with pytest.raises(backup.BackupError, match="damaged"):
        backup.read(bad, rk)
    cut = host["tmp"] / "cut.nvbak"
    cut.write_bytes(f.read_bytes()[:-40])
    with pytest.raises(backup.BackupError):
        backup.read(cut, rk)


def test_restore_in_place_and_on_a_new_host(host):
    cfg = host["cfg"]
    rk = recovery.init(cfg)
    f = Path(backup.create(rk, host["tmp"] / "out")["file"])
    key = vault.load_key(cfg)
    home = host["home"]
    (home / ".rudder/tickets/T1.json").write_text("changed")
    res = backup.restore(f, rk)
    assert (home / ".rudder/tickets/T1.json").read_text() == '{"id": "T1"}'
    assert any(".pre-restore-" in m for m in res["moved_aside"])
    # a new host: no key, no recovery file, no settings. The file and the recovery key are enough.
    import shutil
    shutil.rmtree(home / ".config")
    shutil.rmtree(host["data"])
    backup.restore(f, rk)
    assert vault.load_key(aconfig.load()) == key
    recovery.check(aconfig.load(), rk)
    assert vault.check(aconfig.load())[0][1]             # the credentials open again


def test_cli_asks_and_confirms_the_key(host):
    lines = []
    keys = []

    def out(s=""):
        lines.append(s)
        m = re.search(r"(\d{6}-){7}\d{6}", s)
        if m:
            keys.append(m.group(0))

    assert backup.main(["recovery", "init"], out=out, ask=lambda p: keys[0]) == 0
    assert "Confirmed." in lines
    rk = keys[0]
    assert backup.main(["recovery", "check"], out=out, ask=lambda p: rk) == 0
    assert backup.main(["recovery", "check"], out=out, ask=lambda p: recovery.new_recovery_key()) == 2
    lines.clear()
    assert backup.main(["backup", "--out", str(host["tmp"] / "b")], out=out, ask=lambda p: rk) == 0
    f = next((host["tmp"] / "b").glob("*.nvbak"))
    assert backup.main(["restore", str(f), "--list"], out=out, ask=lambda p: rk) == 0
    assert backup.main(["restore", str(f)], out=out, ask=lambda p: rk, confirm=lambda p: "no") == 1
    log = (host["home"] / ".navigator/settings.log").read_text()
    assert "backup" in log and rk not in log and rk.replace("-", "") not in log
