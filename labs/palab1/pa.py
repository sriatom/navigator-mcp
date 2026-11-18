"""palab1 PAN-OS helper (netmiko). Usage:
  pa.py show  <FW|all> "<op command>" ["<op command>" ...]
  pa.py set   <FW> <file-with-set-lines>        # configure, load lines, commit
Lab login admin / admin. Needs: pip install netmiko"""
import sys, time
from netmiko import ConnectHandler

FW = {"PA-EDGE": "172.31.12.21", "PA-DC": "172.31.12.22", "PA-BR": "172.31.12.23"}


def conn(name):
    return ConnectHandler(device_type="paloalto_panos", host=FW[name], username="admin",
                          password="admin", fast_cli=False, conn_timeout=30, read_timeout_override=120)


def show(name, cmds):
    c = conn(name)
    for cmd in cmds:
        print(f"===== {name}: {cmd}")
        print(c.send_command(cmd, read_timeout=120))
    c.disconnect()


def setcfg(name, path):
    lines = [l.strip() for l in open(path) if l.strip().startswith(("set ", "delete ", "move ", "rename "))]
    c = conn(name)
    c.config_mode()
    errs = []
    for l in lines:
        out = c.send_command(l, expect_string=r"#", read_timeout=60)
        if out.strip() and ("Invalid" in out or "rror" in out or "Unknown" in out or "failed" in out.lower()):
            errs.append((l, out.strip()))
    print(f"{name}: {len(lines)} lines, {len(errs)} errors")
    for l, o in errs:
        print("  ERR:", l, "\n      ", o.replace("\n", " | ")[:300])
    out = c.commit(read_timeout=900)
    print("commit:", out.strip().replace("\n", " | ")[-300:])
    c.disconnect()


if __name__ == "__main__":
    mode, target = sys.argv[1], sys.argv[2]
    names = list(FW) if target == "all" else [target]
    for n in names:
        try:
            show(n, sys.argv[3:]) if mode == "show" else setcfg(n, sys.argv[3])
        except Exception as e:
            print(f"{n}: {type(e).__name__}: {e}")
