"""project action=save, notes for search, and read_files' input limit (DEMO2 findings, 2026-11-19)."""
import pytest

from navigator_engineer import project_files as PF
from navigator_engineer.models import read_files


def _proj(tmp_path):
    p = tmp_path / "JOB"
    (p / "CONFIG").mkdir(parents=True)
    (p / "CONFIG" / "a-C9500.txt").write_text("hostname a\n" + "!\n" * 3000)
    (p / "CONFIG" / "b-93360.txt").write_text("hostname b\nfeature hsrp\n")
    (p / "DEVICES.txt").write_text("b: NX-OS 10.4\n")
    (p / "RUNLOG.md").write_text("log\n")
    return p


def test_save_writes_to_out_and_never_overwrites(tmp_path):
    p = _proj(tmp_path)
    assert PF.save(p, "b_vlan3102.txt", "hsrp 3102") == {"saved": "OUT/b_vlan3102.txt", "bytes": 10}
    assert PF.save(p, "b_vlan3102.txt", "x\n")["saved"] == "OUT/b_vlan3102-1.txt"
    assert (p / "OUT" / "b_vlan3102.txt").read_text() == "hsrp 3102\n"
    for bad in ("../x.txt", "OUT/x.txt", ".hidden", "", "a..b"):
        with pytest.raises(PF.ProjectFilesError, match="plain file name"):
            PF.save(p, bad, "x")


def test_notes_are_the_top_level_files(tmp_path):
    assert PF.notes(_proj(tmp_path)) == [("DEVICES.txt", "b: NX-OS 10.4\n")]


def test_read_files_puts_notes_first_and_reports_what_is_left_out(tmp_path):
    text, used, left = read_files(_proj(tmp_path), None, 1000)
    assert used[0] == "DEVICES.txt" and text.startswith("===== DEVICES.txt")
    assert "CONFIG/a-C9500.txt (cut)" in left and "CONFIG/b-93360.txt" in left
