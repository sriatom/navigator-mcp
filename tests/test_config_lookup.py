"""Where each layer finds its settings: a source checkout, a pip install with user files, or nothing yet."""
import pytest

from atlas_inventory import config as aconfig
from navigator_engineer import config as nconfig
from rudder_devices import config as rconfig


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A pip install: the package's parent has no pyproject.toml; HOME is empty."""
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "bootstrap.yml").write_text("DATA_PATH: /should/not/be/used\n")   # stray file next to packages
    home = tmp_path / "home"
    home.mkdir()
    for k in ("ATLAS_BOOTSTRAP", "NAVI_BOOTSTRAP", "NAVI_MCP_BOOTSTRAP", "NAVIGATOR_BOOTSTRAP", "RUDDER_BOOTSTRAP",
              "RUDDER_LOCAL", "ATLAS_MODE", "ATLAS_CONSUMER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    for m in (aconfig, nconfig, rconfig):
        monkeypatch.setattr(m, "REPO", site)
    return home


def test_pip_install_with_no_settings_uses_the_defaults(installed):
    a = aconfig.load()
    assert a.bootstrap is None and a.data_path == installed / ".atlas" / "data"
    assert a.state_path == installed / ".atlas" and a.consumer == "NAVI"
    assert nconfig.bootstrap_file() == installed / ".config/navigator/navigator.yml"
    assert nconfig.load().workspace == installed / "workspace"
    assert rconfig.bootstrap_file() == installed / ".config/rudder/rudder.yml"
    rconfig.load()


def test_pip_install_reads_the_user_files(installed):
    (installed / ".config/atlas").mkdir(parents=True)
    (installed / ".config/atlas/bootstrap.yml").write_text("DATA_PATH: ~/inv\nCONSUMER: lab\n")
    (installed / ".config/navigator").mkdir(parents=True)
    (installed / ".config/navigator/navigator.yml").write_text("PROJECT_ROOT: ~/jobs\n")
    a = aconfig.load()
    assert a.data_path == installed / "inv" and a.consumer == "LAB"
    assert nconfig.load().workspace == installed / "jobs"


def test_a_checkout_uses_its_own_files(installed, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "bootstrap.yml").write_text("DATA_PATH: ~/checkout-data\n")
    (repo / "navigator.yml").write_text("PROJECT_ROOT: ~/checkout-jobs\n")
    for m in (aconfig, nconfig, rconfig):
        monkeypatch.setattr(m, "REPO", repo)
    assert aconfig.load().data_path == installed / "checkout-data"
    assert nconfig.load().workspace == installed / "checkout-jobs"


def test_the_environment_wins_and_must_exist(installed, tmp_path, monkeypatch):
    f = tmp_path / "b.yml"
    f.write_text("DATA_PATH: ~/env-data\n")
    monkeypatch.setenv("ATLAS_BOOTSTRAP", str(f))
    assert aconfig.load().data_path == installed / "env-data"
    monkeypatch.setenv("ATLAS_BOOTSTRAP", str(tmp_path / "missing.yml"))
    with pytest.raises(FileNotFoundError):
        aconfig.load()
