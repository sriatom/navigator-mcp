"""P6: vManage reads, Cisco IOS-XE / NX-OS changes (checkpoint + rollback), PAN-OS changes (SSH candidate
edits + XML API diff / commit / rollback). All against fakes: no devices, no network."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import SECRET
from rudder_devices import adapters, approval
from rudder_devices.changes import Changes
from rudder_devices.cisco import CiscoCliWriter, line_paths, plan_diff
from rudder_devices.config import Settings
from rudder_devices.core import Rudder, RudderError
from rudder_devices.panos import PanosWriter
from rudder_devices.vmanage import Vmanage
from test_p3 import rig  # noqa: F401

P6_COMMANDS = """\
ARISTA_EOS:
  system: [show version]
CISCO_IOSXE:
  interfaces: [show ip interface brief]
PA_PANOS:
  system: [show system info]
VIPTELA:
  system: [show devices, show system status, show running-config]
  control: [show control connections]
"""

INV_P6 = """\
INVENTORY:
- {DEVICE_ID: R1, DEVICE_NAME: R1, DEVICE_ADDR: 10.6.0.1, CREDENTIAL: FAB, VENDOR: CISCO, OS: IOSXE,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: N1, DEVICE_NAME: N1, DEVICE_ADDR: 10.6.0.2, CREDENTIAL: FAB, VENDOR: CISCO, OS: NXOS,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: X1, DEVICE_NAME: X1, DEVICE_ADDR: 10.6.0.3, CREDENTIAL: FAB, VENDOR: CISCO, OS: IOSXR,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: PA1, DEVICE_NAME: PA1, DEVICE_ADDR: 10.6.0.4, CREDENTIAL: LAB_PANOS, VENDOR: PALOALTO, OS: PANOS,
   CONN_TYPE: SSH, PORT: 22, IS_ENABLED: true}
- {DEVICE_ID: L1, DEVICE_NAME: L1, DEVICE_ADDR: 10.6.0.5, CREDENTIAL: FAB, VENDOR: ARISTA, OS: EOS,
   CONN_TYPE: EAPI, PORT: 443, IS_ENABLED: true}
- {DEVICE_ID: VE1, DEVICE_NAME: VE1, DEVICE_ADDR: 10.6.0.6, CREDENTIAL: FAB, VENDOR: CISCO, OS: VIPTELA,
   CONN_TYPE: VMANAGE_API, PORT: 443, VMANAGE: 10.6.0.100, SYSTEM_IP: 1.1.1.6, IS_ENABLED: true}
- {DEVICE_ID: VE2, DEVICE_NAME: VE2, DEVICE_ADDR: 10.6.0.7, CREDENTIAL: FAB, VENDOR: CISCO, OS: VIPTELA,
   CONN_TYPE: VMANAGE_API, PORT: 443, VMANAGE: 10.6.0.100, IS_ENABLED: true}
"""


# =========================================================================== vManage
class FakeVmanage:
    def __init__(self):
        self.logged_in, self.calls, self.password = False, [], SECRET

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, req.url.query.decode()))
        if req.url.path == "/j_security_check":
            form = parse_qs(req.content.decode())
            if form.get("j_password") == [self.password]:
                self.logged_in = True
                return httpx.Response(200, text="")
            return httpx.Response(200, text="<html><body>login</body></html>")
        if not self.logged_in:
            return httpx.Response(401)
        if req.url.path == "/dataservice/client/token":
            return httpx.Response(200, text="XSRF123")
        if req.url.path == "/logout":
            return httpx.Response(200, text="")
        assert req.headers.get("X-XSRF-TOKEN") == "XSRF123"
        if req.url.path == "/dataservice/device":
            return httpx.Response(200, json={"header": {}, "data": [{"host-name": "vedge1"}]})
        if req.url.path == "/dataservice/device/system/status":
            return httpx.Response(200, json={"data": [{"version": "20.9", "deviceId": req.url.params["deviceId"]}]})
        if req.url.path == "/dataservice/device/config":
            return httpx.Response(200, text="system\n host-name vedge1\n")
        return httpx.Response(404)


@pytest.fixture()
def p6(rig):  # noqa: F811
    (rig["tmp"] / "catalog" / "COMMANDS.yml").write_text(P6_COMMANDS)
    (rig["vault"] / "INVENTORY_P6.yml").write_text(INV_P6)
    return rig


def test_vmanage_reads(p6):
    vm = FakeVmanage()
    fac = {"vmanage": lambda: Vmanage(p6["s"], client=lambda **kw: httpx.Client(transport=httpx.MockTransport(vm), **kw))}
    r = Rudder(p6["s"], factories=fac).read("P6", devices=["VE1", "VE2"], category="system")
    by = {d["device"]: d for d in r["devices"]}
    ve1 = {x["command"]: x for x in by["VE1"]["results"]}
    assert by["VE1"]["adapter"] == "native:vmanage" and by["VE1"]["status"] == "ok"
    assert ve1["show devices"]["format"] == "json" and ve1["show devices"]["output"] == [{"host-name": "vedge1"}]
    assert ve1["show system status"]["output"][0]["deviceId"] == "1.1.1.6"
    assert ve1["show running-config"]["format"] == "text" and "host-name vedge1" in ve1["show running-config"]["output"]
    ve2 = {x["command"]: x for x in by["VE2"]["results"]}
    assert ve2["show devices"]["ok"] and not ve2["show system status"]["ok"]
    assert "SYSTEM_IP" in ve2["show system status"]["error"]
    assert ("GET", "/logout", "nocache=1") in vm.calls
    assert all(SECRET not in q for _, _, q in vm.calls) and SECRET not in json.dumps(r)


def test_vmanage_bad_login(p6):
    vm = FakeVmanage()
    vm.password = "other"
    fac = {"vmanage": lambda: Vmanage(p6["s"], client=lambda **kw: httpx.Client(transport=httpx.MockTransport(vm), **kw))}
    d = Rudder(p6["s"], factories=fac).read("P6", devices=["VE1"], category="control")["devices"][0]
    assert d["status"] == "auth_failed"


# =========================================================================== Cisco diff planner
RUNNING_XE = """\
Building configuration...

Current configuration : 1234 bytes
!
hostname r1
!
interface Loopback0
 ip address 10.0.0.1 255.255.255.255
!
interface GigabitEthernet2
 description uplink
 shutdown
!
ip domain lookup
end
"""


def test_line_paths_indent_and_heuristic():
    assert line_paths(["interface Loopback10", " description x"]) == [((), "interface Loopback10"),
                                                                      (("interface Loopback10",), "description x")]
    assert line_paths(["interface Loopback10", "ip address 1.1.1.1 255.255.255.255", "ip route 0.0.0.0 0.0.0.0 1.1.1.2"]) \
        == [((), "interface Loopback10"), (("interface Loopback10",), "ip address 1.1.1.1 255.255.255.255"),
            ((), "ip route 0.0.0.0 0.0.0.0 1.1.1.2")]


def test_plan_diff():
    assert plan_diff(RUNNING_XE, ["interface Loopback10", " description rudder"]) == \
        "+interface Loopback10\n+ description rudder\n"
    # existing parent shown as context; case-insensitive; existing line is not a change
    assert plan_diff(RUNNING_XE, ["interface gigabitethernet2", " description uplink", " no shutdown"]) == \
        " interface GigabitEthernet2\n- shutdown\n"
    assert plan_diff(RUNNING_XE, ["interface Loopback0", " no ip address"]) == \
        " interface Loopback0\n- ip address 10.0.0.1 255.255.255.255\n"
    assert plan_diff(RUNNING_XE, ["no interface Loopback0"]) == \
        "-interface Loopback0\n- ip address 10.0.0.1 255.255.255.255\n"
    assert plan_diff(RUNNING_XE, ["no ip domain lookup"]) == "-ip domain lookup\n"
    assert plan_diff(RUNNING_XE, ["no ip http server"]).startswith("~no ip http server")
    assert plan_diff(RUNNING_XE, ["hostname r1"]) == ""


# =========================================================================== Cisco fake box
class FakeCisco:
    """A running config as (path) lines, checkpoint files, configure replace, NX-OS checkpoints."""

    def __init__(self, nxos=False):
        self.nxos = nxos
        self.cfg = ["hostname r1", "interface Loopback0", " ip address 10.0.0.1 255.255.255.255"]
        self.files: dict[str, list[str]] = {}
        self.saved = False
        self.sent: list[str] = []

    def text(self):
        return "Building configuration...\n!\n" + "\n".join(self.cfg) + "\nend\n"

    def add(self, lines):
        for parent, line in line_paths(lines):
            m = re.match(r"^no\s+(.*)$", line)
            if parent:
                i = self.cfg.index(parent[0]) if parent[0] in self.cfg else None
                if i is None:
                    self.cfg.append(parent[0])
                    i = len(self.cfg) - 1
                j = i + 1
                while j < len(self.cfg) and self.cfg[j].startswith(" "):
                    j += 1
                if m:
                    self.cfg = [c for c in self.cfg if not (c.startswith(" " + m.group(1)))]
                elif " " + line not in self.cfg[i:j]:
                    self.cfg.insert(j, " " + line)
            elif m:
                self.cfg = [c for c in self.cfg if c != m.group(1)]
            elif line not in self.cfg:
                self.cfg.append(line)

    def conn(self):
        box = self

        class C:
            def send_command(self, cmd, read_timeout=None):
                box.sent.append(cmd)
                if cmd == "show running-config":
                    return box.text()
                if cmd == "show ip interface brief":
                    return "\n".join(c for c in box.cfg if c.startswith("interface"))
                return "% Invalid input detected"

            def send_command_timing(self, cmd, read_timeout=None):
                box.sent.append(cmd)
                if cmd.startswith("copy running-config "):
                    box._pending = cmd.split()[-1]
                    return f"Destination filename [{box._pending.split(':')[-1]}]? "
                if cmd == "\n" and getattr(box, "_pending", None):
                    box.files[box._pending] = list(box.cfg)
                    box._pending = None
                    return "1234 bytes copied in 0.1 secs"
                if cmd.startswith("checkpoint "):
                    box.files[cmd.split()[1]] = list(box.cfg)
                    return "Done"
                if cmd.startswith("configure replace "):
                    box.cfg = list(box.files[cmd.split()[2]])
                    return "Total number of passes: 1\nRollback Done"
                if cmd.startswith("rollback running-config checkpoint "):
                    box.cfg = list(box.files[cmd.split()[-1]])
                    return "Collecting Running-Config\nRollback completed successfully."
                if cmd.startswith(("delete /force ", "no checkpoint ")):
                    box.files.pop(cmd.split()[-1], None)
                    return ""
                return "% Invalid input detected"

            def send_config_set(self, lines, cmd_verify=False, read_timeout=None):
                box.sent += ["<config>"] + list(lines)
                good = []
                for ln in lines:
                    if "bogus" in ln:
                        box.add(good)
                        return "\n".join(good) + f"\n{ln}\n% Invalid input detected at '^' marker.\n"
                    good.append(ln)
                box.add(lines)
                return "\n".join(lines)

            def save_config(self):
                box.saved = True
                return "[OK]"

            def disconnect(self):
                pass
        return C()


@pytest.fixture()
def cx(p6):
    box = FakeCisco()
    sent = []

    def runner(argv, input=None, **kw):
        sent.append(input)
        return type("R", (), {"returncode": 0})()

    appr = p6["tmp"] / "push-approvals"
    chan = approval.HermesChannel({"APPROVAL_DIR": str(appr), "PROFILES": {"NAVI": "navi"}, "HERMES_BIN": "/bin/true"},
                                  "NAVI", runner=runner)
    s = Settings(**{**p6["s"].__dict__, "state_path": p6["tmp"] / "rudder-state", "approval": {"TTL_MIN": 30}})
    fac = {"cisco-writer": lambda: CiscoCliWriter(s, "cisco_xe", connect=lambda *a: box.conn())}
    return {"c": Changes(s, factories=fac, channel=chan), "box": box, "sent": sent, "dir": appr, "s": s,
            "chan": chan, "p6": p6}


def _approve(ctx, verdict="approved"):
    code = re.search(r"/pushok (\w+)", ctx["sent"][-1]).group(1)
    (ctx["dir"] / approval.code_hash("decision", code)).write_text(verdict)
    (ctx["dir"] / approval.code_hash("pending", code)).unlink()


def test_cisco_change_applied_with_checkpoint(cx):
    st = cx["c"].propose("P6", ["R1"], ["interface Loopback10", " description rudder"], "lo10",
                         verify_commands=["show ip interface brief"], expect={"show ip interface brief": ["Loopback10"]},
                         save=True)
    assert st["devices"][0]["diff"] == "+interface Loopback10\n+ description rudder\n"
    assert st["devices"][0]["adapter"] == "native:netmiko-cli"
    assert "<config>" not in cx["box"].sent                    # the dry run sent nothing but show commands
    _approve(cx)
    done = cx["c"].apply(st["ticket"])
    assert done["state"] == "applied", done
    d = done["devices"][0]
    assert d["checkpoint"] == f"bootflash:{'rudder-' + st['ticket'].lower()}.cfg"
    assert "interface Loopback10" in cx["box"].cfg and " description rudder" in cx["box"].cfg
    assert cx["box"].files == {} and cx["box"].saved                 # checkpoint removed after confirm
    assert any("rollback point" in ln for ln in done["log"])


def test_cisco_refused_line_rolls_back(cx):
    before = list(cx["box"].cfg)
    st = cx["c"].propose("P6", ["R1"], ["interface Loopback11", " description a", " bogus command"], "bad")
    _approve(cx)
    r = cx["c"].apply(st["ticket"])
    assert r["state"] == "failed" and r["devices"][0]["state"] == "failed"
    assert "refused a line" in r["devices"][0]["error"] and "rolled back" in r["devices"][0]["error"]
    assert cx["box"].cfg == before


def test_cisco_verify_failure_rolls_back(cx):
    before = list(cx["box"].cfg)
    st = cx["c"].propose("P6", ["R1"], ["interface Loopback12"], "lo12", verify_commands=["show ip interface brief"],
                         expect={"show ip interface brief": ["Loopback99"]})
    _approve(cx)
    r = cx["c"].apply(st["ticket"])
    assert r["devices"][0]["state"] == "rolled_back" and cx["box"].cfg == before and cx["box"].files == {}


def test_cisco_nxos_checkpoint(cx):
    box = FakeCisco(nxos=True)
    s = cx["s"]
    c = Changes(s, factories={"cisco-writer": lambda: CiscoCliWriter(s, "cisco_nxos", connect=lambda *a: box.conn())},
                channel=cx["chan"])
    st = c.propose("P6", ["N1"], ["interface loopback10"], "nx lo10")
    _approve(cx)
    r = c.apply(st["ticket"])
    assert r["state"] == "applied" and "checkpoint " + f"rudder-{st['ticket'].lower()}" in box.sent


def test_one_kind_per_ticket_and_unsupported(cx):
    with pytest.raises(RudderError, match="one kind of device"):
        cx["c"].propose("P6", ["R1", "PA1"], ["interface Loopback10"], "mixed")
    with pytest.raises(RudderError, match="IOS-XR changes"):
        cx["c"].propose("P6", ["X1"], ["interface Loopback10"], "xr")
    with pytest.raises(RudderError, match="deny list"):
        cx["c"].propose("P6", ["R1"], ["do reload"], "sneaky")
    with pytest.raises(RudderError, match="deny list"):
        cx["c"].propose("P6", ["R1"], ["line vty 0 4", " transport input none"], "lockout")


# =========================================================================== PAN-OS fake firewall
class FakePanos:
    """Addresses only: running / candidate dicts, saved files, a commit job, and the SSH config mode."""

    def __init__(self):
        self.running = {"web": "10.0.0.10/32"}
        self.cand = dict(self.running)
        self.saved: dict[str, dict] = {}
        self.fail_commit = False
        self.key_requests = 0
        self.ssh: list[str] = []

    def xml(self, cfg: dict, dirty=False) -> str:
        extra = ' admin="admin" dirtyId="4" time="2026/11/11"' if dirty else ""
        ents = "".join(f'<entry name="{n}"{extra}><ip-netmask>{ip}</ip-netmask></entry>'
                       for n, ip in sorted(cfg.items()))
        return ('<config version="11.2.0"><mgt-config><users><entry name="admin"><phash>$5$secret-hash</phash>'
                '</entry></users></mgt-config><devices><entry name="localhost.localdomain"><vsys>'
                f'<entry name="vsys1"><address>{ents}</address></entry></vsys></entry></devices></config>')

    @staticmethod
    def ok(inner="") -> httpx.Response:
        return httpx.Response(200, text=f'<response status="success"><result>{inner}</result></response>')

    def __call__(self, req: httpx.Request) -> httpx.Response:
        f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        assert "key=" not in str(req.url) and "password" not in str(req.url)
        if f["type"] == "keygen":
            self.key_requests += 1
            if f.get("password") != SECRET:
                return httpx.Response(403, text='<response status="error"><result><msg>Invalid Credential</msg>'
                                                '</result></response>')
            return self.ok("<key>APIKEY1</key>")
        assert f.get("key") == "APIKEY1"
        if f["type"] == "config":
            return httpx.Response(200, text='<response status="error"><msg>not used</msg></response>')
        if f.get("cmd") in ("<show><config><running></running></config></show>",
                            "<show><config><candidate></candidate></config></show>"):
            cand = "candidate" in f["cmd"]
            src = self.cand if cand else self.running
            return self.ok(self.xml(src, dirty=cand and src != self.running))
        if f["type"] == "commit":
            if self.cand == self.running:
                return httpx.Response(200, text='<response status="success" code="19"><msg>There are no changes '
                                                'to commit.</msg></response>')
            self.job = "FAIL" if self.fail_commit else "OK"
            if not self.fail_commit:
                self.running = dict(self.cand)
            return self.ok("<msg><line>Commit job enqueued</line></msg><job>7</job>")
        cmd = f["cmd"]
        if cmd.startswith("<check><pending-changes>"):
            return self.ok("yes" if self.cand != self.running else "no")
        if cmd == "<load><config><from>running-config.xml</from></config></load>":
            self.cand = dict(self.running)
            return self.ok("Config loaded")
        m = re.match(r"<load><config><from>(.+)</from></config></load>", cmd)
        if m:
            self.cand = dict(self.saved[m.group(1)])
            return self.ok("Config loaded")
        m = re.match(r"<save><config><to>(.+)</to></config></save>", cmd)
        if m:
            self.saved[m.group(1)] = dict(self.cand)
            return self.ok("Config saved")
        m = re.match(r"<delete><config><saved>(.+)</saved></config></delete>", cmd)
        if m:
            self.saved.pop(m.group(1), None)
            return self.ok("deleted")
        if cmd.startswith("<show><jobs><id>7"):
            return self.ok(f"<job><status>FIN</status><result>{self.job}</result>"
                           f"<details><line>{'rule x is invalid' if self.job == 'FAIL' else 'ok'}</line></details></job>")
        return httpx.Response(200, text='<response status="error"><msg>unknown</msg></response>')

    def conn(self):
        fw = self

        class C:
            def config_mode(self):
                fw.ssh.append("configure")

            def exit_config_mode(self):
                fw.ssh.append("exit")

            def send_command_timing(self, ln, read_timeout=None):
                fw.ssh.append(ln)
                m = re.match(r"^set address (\S+) ip-netmask (\S+)$", ln)
                if m:
                    fw.cand[m.group(1)] = m.group(2)
                    return ""
                m = re.match(r"^delete address (\S+)$", ln)
                if m:
                    fw.cand.pop(m.group(1), None)
                    return ""
                return "Invalid syntax."

            def send_command(self, cmd, read_timeout=None):
                return "hostname: PA1\naddresses: " + ",".join(sorted(fw.running))

            def disconnect(self):
                pass
        return C()


@pytest.fixture()
def pa(cx):
    fw = FakePanos()
    s = cx["s"]
    fac = {"panos-writer": lambda: PanosWriter(s, client=lambda **kw: httpx.Client(transport=httpx.MockTransport(fw), **kw),
                                               connect=lambda *a: fw.conn(), sleep=lambda x: None)}
    return {**cx, "c": Changes(s, factories=fac, channel=cx["chan"]), "fw": fw}


ADD = ["set address h-10.1.1.1 ip-netmask 10.1.1.1/32"]


def test_panos_change_applied(pa):
    st = pa["c"].propose("P6", ["PA1"], ADD, "add host", verify_commands=["show system info"],
                         expect={"show system info": ["h-10.1.1.1"]})
    diff = st["devices"][0]["diff"]
    assert re.search(r'^\+\s+<entry name="h-10.1.1.1">$', diff, re.M), diff
    assert "dirtyId" not in diff and 'admin="' not in diff
    assert pa["fw"].cand == pa["fw"].running == {"web": "10.0.0.10/32"}       # dry run left nothing behind
    _approve(pa)
    r = pa["c"].apply(st["ticket"])
    assert r["state"] == "applied", r
    assert pa["fw"].running == {"web": "10.0.0.10/32", "h-10.1.1.1": "10.1.1.1/32"}
    assert pa["fw"].saved == {}                                                # rollback file removed
    ticket = (pa["s"].state_path / "tickets" / f"{st['ticket']}.json").read_text()
    assert "APIKEY1" not in ticket and SECRET not in ticket and "$5$secret-hash" not in json.dumps(r)


def test_panos_secrets_hashed_in_diff():
    from rudder_devices.panos import _canon
    import xml.etree.ElementTree as ET
    text = "\n".join(_canon(ET.fromstring(FakePanos().xml({"a": "1.1.1.1/32"}))))
    assert "$5$secret-hash" not in text and "<phash>hidden:" in text


def test_panos_pending_changes_refused(pa):
    pa["fw"].cand["someone"] = "10.9.9.9/32"
    with pytest.raises(RudderError, match="uncommitted changes"):
        pa["c"].propose("P6", ["PA1"], ADD, "add host")
    assert not list((pa["s"].state_path / "locks").iterdir())


def test_panos_commit_failure_reverts(pa):
    st = pa["c"].propose("P6", ["PA1"], ADD, "add host")
    _approve(pa)
    pa["fw"].fail_commit = True
    r = pa["c"].apply(st["ticket"])
    assert r["state"] == "failed" and "commit job 7 failed" in r["devices"][0]["error"]
    assert pa["fw"].cand == pa["fw"].running == {"web": "10.0.0.10/32"}


def test_panos_verify_failure_rolls_back(pa):
    st = pa["c"].propose("P6", ["PA1"], ["delete address web"], "remove web",
                         verify_commands=["show system info"], expect={"show system info": ["never-there"]})
    _approve(pa)
    r = pa["c"].apply(st["ticket"])
    assert r["devices"][0]["state"] == "rolled_back"
    assert pa["fw"].running == {"web": "10.0.0.10/32"} and pa["fw"].saved == {}


@pytest.mark.parametrize("bad", ["set mgt-config users evil password", "commit", "delete address",
                                 "delete rulebase security", "set deviceconfig system ip-address 1.2.3.4",
                                 "request restart system", "show system info"])
def test_panos_deny_list(pa, bad):
    with pytest.raises(RudderError, match="deny list"):
        pa["c"].propose("P6", ["PA1"], [bad], "x")


def test_panos_refused_line(pa):
    with pytest.raises(RudderError, match="refused"):
        pa["c"].propose("P6", ["PA1"], ["set address bad ip-range"], "x")
    assert pa["fw"].cand == pa["fw"].running


def test_panos_bad_credential(pa):
    (pa["p6"]["vault"] / "CREDENTIALS.yml").write_text(
        (pa["p6"]["vault"] / "CREDENTIALS.yml").read_text().replace(f"TAG: LAB_PANOS, USERNAME: admin, PASSWORD: {SECRET}",
                                                                    "TAG: LAB_PANOS, USERNAME: admin, PASSWORD: wrong"))
    with pytest.raises(RudderError, match="refused the credential"):
        pa["c"].propose("P6", ["PA1"], ADD, "x")


def test_adapter_choice():
    s = Settings(atlas_socket=None, token_file=None, consumer="NAVI", records_path=None, eapi_verify_tls=False,
                 ssh_strict=False, known_hosts=None, timeout=5, max_workers=1, max_inline_chars=1000)
    w, _ = adapters.choose_writer({"ID": "IOSXE", "writes": "native:netmiko", "netmiko": "cisco_xe"},
                                  {"CONN_TYPE": "SSH"}, s)
    assert isinstance(w, CiscoCliWriter)
    w, why = adapters.choose_writer({"ID": "IOSXE", "writes": "native:netmiko", "netmiko": "cisco_xe"},
                                    {"CONN_TYPE": "RESTCONF"}, s)
    assert w is None and "SSH" in why
    w, _ = adapters.choose_writer({"ID": "PANOS", "writes": "native:xml-api"}, {"CONN_TYPE": "SSH"}, s)
    assert isinstance(w, PanosWriter)
    r, _ = adapters.choose({"ID": "VIPTELA", "reads": "native:vmanage"}, {"CONN_TYPE": "VMANAGE_API"}, s)
    assert isinstance(r, Vmanage)
