"""A fake NAVI.DATA folder: two inventories and a credential file with a known test secret."""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

SECRET = "S3cr3t-TEST-do-not-print"

INV_LAB = """\
INVENTORY:
- {DEVICE_ID: PA-EDGE, DEVICE_NAME: PA-EDGE, DEVICE_ADDR: 198.18.20.21, CREDENTIAL: LAB_PANOS,
   VENDOR: PALOALTO, OS: PANOS, CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true,
   TAGS: {SITE: HQ, FAB: PALAB1, ROLE: [FIREWALL, EDGE]}}
- {DEVICE_ID: CORE1, DEVICE_NAME: CORE1, DEVICE_ADDR: 198.18.20.12, CREDENTIAL: LAB_EOS,
   VENDOR: ARISTA, OS: ARISTA_EOS, CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true,
   TAGS: {SITE: HQ, FAB: PALAB1, ROLE: [CORE]}}
- {DEVICE_ID: OLD1, DEVICE_NAME: OLD1, DEVICE_ADDR: 198.18.20.99, CREDENTIAL: LAB_EOS,
   VENDOR: ARISTA, OS: EOS, CONN_TYPE: SSH, PORT: 22, IS_ENABLED: false, TAGS: {SITE: HQ}}
"""

INV_FAB = """\
INVENTORY:
- {DEVICE_ID: QALEAF1A, DEVICE_NAME: QALEAF1A, DEVICE_ADDR: 198.19.0.11, CREDENTIAL: FAB,
   VENDOR: ARISTA, OS: EOS, CONN_TYPE: EAPI, PORT: 443, IS_ENABLED: true,
   TAGS: {SITE: QA, FAB: EVPN, ROLE: [LEAF]}}
- {DEVICE_ID: PDLEAF1A, DEVICE_NAME: PDLEAF1A, DEVICE_ADDR: 198.19.0.21, CREDENTIAL: FAB,
   VENDOR: ARISTA, OS: EOS, CONN_TYPE: EAPI, PORT: 443, IS_ENABLED: true,
   TAGS: {SITE: PD, FAB: EVPN, ROLE: [LEAF, PRODUCTION]}}
"""

CREDS = f"""\
DATA:
- {{TAG: LAB_PANOS, USERNAME: admin, PASSWORD: {SECRET}}}
- {{TAG: FAB, USERNAME: labuser, PASSWORD: {SECRET}, ENABLE_PASSWORD: ''}}
"""


COMMANDS = """\
ARISTA_EOS:
  system: [show version, show hostname]
  bgp: [show ip bgp summary]
CISCO_NXOS:
  system: [show version]
"""

PKG_CATALOG = Path(__file__).resolve().parent.parent / "atlas_inventory" / "catalog"


def write_boot(tmp_path, extra: str = "") -> None:
    """(Re)write the test bootstrap. extra = more YAML lines (KEY_FILE_2, CONSUMERS, ...)."""
    data = tmp_path / "NAVI.DATA"
    cat = tmp_path / "catalog"
    if not cat.is_dir():
        cat.mkdir()
        shutil.copy(PKG_CATALOG / "PLATFORMS.yml", cat / "PLATFORMS.yml")
        (cat / "COMMANDS.yml").write_text(COMMANDS)
    (tmp_path / "bootstrap.yml").write_text(textwrap.dedent(f"""\
        DATA_PATH: {data}
        CREDENTIAL_FILE: CREDENTIALS.yml
        STATE_PATH: {tmp_path / 'state'}
        KEY_FILE: {tmp_path / 'keys' / 'vault.key'}
        CATALOG_PATH: {cat}
        """) + textwrap.dedent(extra))


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    data = tmp_path / "NAVI.DATA"
    data.mkdir()
    (data / "INVENTORY_PALAB.yml").write_text(INV_LAB)
    (data / "INVENTORY_FAB.yml").write_text(INV_FAB)
    (data / "CREDENTIALS.yml").write_text(CREDS)
    (data / "COMMAND_LIST.yml").write_text("x: 1\n")
    write_boot(tmp_path)
    monkeypatch.setenv("ATLAS_BOOTSTRAP", str(tmp_path / "bootstrap.yml"))
    for var in ("ATLAS_MODE", "ATLAS_CONSUMER", "NAVI_MODE", "NAVI_CONSUMER", "NAVI_MCP_BOOTSTRAP"):
        monkeypatch.delenv(var, raising=False)
    return data


@pytest.fixture(autouse=True)
def _no_host_tls(tmp_path, monkeypatch):
    """Tests never use the host's real TLS files (change pages would switch to HTTPS)."""
    monkeypatch.setenv("NAVIGATOR_TLS_DIR", str(tmp_path / "no-tls"))
