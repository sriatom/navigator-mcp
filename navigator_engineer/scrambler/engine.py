"""Scrambler engine: LOG-SANITIZER's engine (a skills folder/LOG-SANITIZER/lib/log_sanitizer.py, 2026-10-13),
vendored into NAVIGATOR (decision #97) with two modes (decision #99):

  default  secrets only: passwords, type-7, enable secrets, TACACS/RADIUS keys, SNMP communities and v3
           keys, routing auth keys, PSKs, hashes, certificates / private keys -> placeholders such as
           <PASSWORD type=7>. Nothing else changes, so nothing needs restoring.
  deep     default + publicly traceable data, mapped consistently per project (restorable):
           hostnames -> DC1-SW1, domains -> example.net, users -> USER01, emails, AAA groups,
           descriptions / remarks / VLAN names / SNMP location + contact / org / banners, serials, MACs,
           public IPv4 / IPv6 (documentation ranges), public BGP ASNs (64496+), denylist terms.
           level "strict" also maps object / ACL / route-map / VRF / zone / policy names.
           Private (RFC1918 / CGNAT) addresses and private ASNs are kept unless map_private is set.

The CLI of the original is replaced by navigator_engineer.scrambler.api. Rules are unchanged (rules.py).
"""
import argparse
import ipaddress
import json
import os
import re
import sys
from datetime import datetime

from . import rules as R

# ---------------------------------------------------------------------------------------------
# Sentinels: replaced spans are swapped for private-use characters so later passes cannot touch
# them; they are expanded at the very end. Encoding uses no ASCII, so no regex can match inside.
# ---------------------------------------------------------------------------------------------
S_OPEN, S_CLOSE = "\U000F0000", "\U000F0001"
_DIG0, _BASE = 0xE000, 6400
SENT_RE = re.compile(S_OPEN + "([-]+)" + S_CLOSE)


class Tokens:
    def __init__(self):
        self.items, self.stats = [], {}

    def add(self, text, cat):
        i = len(self.items)
        self.items.append(text)
        self.stats[cat] = self.stats.get(cat, 0) + 1
        enc = ""
        while True:
            enc = chr(_DIG0 + i % _BASE) + enc
            i //= _BASE
            if not i:
                break
        return S_OPEN + enc + S_CLOSE

    def expand(self, text):
        def dec(m):
            n = 0
            for ch in m.group(1):
                n = n * _BASE + (ord(ch) - _DIG0)
            return self.items[n]
        return SENT_RE.sub(dec, text)


# ---------------------------------------------------------------------------------------------
# bootstrap.yml (flat "KEY: value  # comment" only — no PyYAML needed)
# ---------------------------------------------------------------------------------------------
def load_bootstrap():
    path = os.environ.get("LOG_SANITIZER_BOOTSTRAP") or os.path.join(os.path.dirname(HERE), "bootstrap.yml")
    cfg = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.split(" #")[0].strip()
            if not line or line.startswith(("#", "---")) or ":" not in line:
                continue
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return cfg


# ---------------------------------------------------------------------------------------------
# Mapping (vault file)
# ---------------------------------------------------------------------------------------------
CI_CATS = {"host", "domain", "email", "user", "serial", "term"}          # case-insensitive categories
FMT = {
    "user": "USER{:02d}", "aaa": "AAA-GRP{:02d}", "vlan": "VLAN-NAME-{:03d}", "desc": "DESC-{:04d}",
    "location": "SITE-LOC-{:02d}", "contact": "CONTACT-{:02d}", "org": "ORG-{:02d}",
    "email": "user{:02d}@example.com", "object": "OBJ-{:04d}",
    "tunnel": "TUNNEL-GRP-{:02d}", "policy": "GRP-POLICY-{:02d}", "serial": "SERIAL-{:04d}",
    "banner": "BANNER-{:02d}",
}
NOT_HOSTS = {"router", "switch", "localhost", "firewall", "ciscoasa", "vedge", "cedge", "fortigate",
             "pa-vm", "nexus", "arista", "ap", "wlc", "host", "hostname", "unknown", "device",
             # common FQDN first labels / product keywords — never a device's own name
             "vbond", "vsmart", "vmanage", "www", "vpn", "mail", "smtp", "ns1", "ns2", "ntp", "dns", "api",
             "apiserver", "portal", "login", "sso", "ldap", "proxy", "time", "pool"}
V4_POOL = ([f"192.0.2", "198.51.100", "203.0.113"] +
           [f"198.{a}.{b}" for a in (18, 19) for b in range(256)])
ASN_POOL = list(range(64496, 64512)) + list(range(65536, 65552)) + list(range(65552, 131072))


ROLE_FW = r'fw|asa|ftd|fpr|firewall|fgt|fortigate|^fg|palo|(^|[-_.])pa[-_\d]|panorama|srx'
ROLE_R_STRONG = (r'rtr|router|vedge|cedge|isr\d|asr\d|csr\d|(^|[-_.])r\d|(^|[-_.])cr\d|(^|[-_.])br\d|(^|[-_.])pe\d|'
                 r'(^|[-_.])ce\d|vsmart|vbond|vmanage|(^|[-_.])gw')
ROLE_SW = (r'sw|switch|leaf|spine|tor\d|(^|[-_.])tor|dist|acc|agg|nx|n\dk|eos|arista|(^|[-_.])ds\d|'
           r'(^|[-_.])as\d|core')
ROLE_R_WEAK = r'wan|edge|inet|isr|asr|csr|(^|[-_.])mx'
# Segments that describe a role/function, not a site (skipped when picking the site prefix).
NOT_SITE = re.compile(r'^(?:\d+|' + "|".join([
    r'v?edge', r'cedge', r'rtr\d*', r'r\d+', r'router', r'sw\d*', r'switch', r'fw\d*', r'firewall', r'leaf\d*',
    r'spine\d*', r'core', r'dist', r'acc', r'agg', r'tor\d*', r'border', r'bl\d*', r'wan', r'inet', r'pa', r'fgt\d*',
    r'asa\d*', r'ftd\d*', r'nx\d*', r'mgmt', r'oob', r'lab', r'prod', r'dev', r'test', r'vsmart', r'vbond',
    r'vmanage', r'gw\d*', r'pe\d*', r'ce\d*', r'cr\d*', r'br\d*', r'ds\d*', r'as\d*']) + r')$', re.I)


def role_of(host):
    h = host.lower()
    for role, rx in (("FW", ROLE_FW), ("R", ROLE_R_STRONG), ("SW", ROLE_SW), ("R", ROLE_R_WEAK)):
        if re.search(rx, h):
            return role
    return "NET"


def site_of(host):
    segs = [s for s in re.split(r'[-_.]', host) if s]
    if len(segs) < 2:
        return "_"
    for s in segs:
        if not NOT_SITE.match(s):
            return s.lower()
    return "_"


class Mapping:
    def __init__(self, path):
        self.path = path
        self.d = {"version": 1, "entries": {}, "counters": {}, "sites": {}, "mac": {}, "ipv4": {},
                  "ipv4_private": {}, "ipv6": {}, "asn": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.d.update(json.load(fh))
        self.corpus_lower = ""
        self.seen_v4_24, self.seen_v4_16, self.seen_v6_48 = set(), set(), set()
        self._index = {}
        for cat, ents in self.d["entries"].items():
            for key, e in ents.items():
                self._index.setdefault(e["real"].lower(), (cat, key))

    def save(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- collision check: a fake must never already exist in the input -------------------------
    def _in_corpus(self, fake, substring=False):
        f = fake.lower()
        if substring:
            return f in self.corpus_lower
        return re.search(r'(?<![a-z0-9])' + re.escape(f) + r'(?![a-z0-9])', self.corpus_lower) is not None

    def _next(self, counter_key, render, substring=False):
        c = self.d["counters"]
        used = {e["fake"].lower() for ents in self.d["entries"].values() for e in ents.values()}
        while True:
            c[counter_key] = c.get(counter_key, 0) + 1
            fake = render(c[counter_key])
            if fake.lower() not in used and not self._in_corpus(fake, substring):
                return fake

    def name(self, cat, real):
        """Return the fake for a real value, creating it if needed."""
        key = real.lower() if cat in CI_CATS and cat != "term" else real
        ents = self.d["entries"].setdefault(cat, {})
        if key in ents:
            return ents[key]["fake"]
        hit = self._index.get(real.lower())                     # same value already mapped elsewhere
        if hit and "term" not in (cat, hit[0]) and \
                (hit[0] in CI_CATS or self.d["entries"][hit[0]][hit[1]]["real"] == real):
            return self.d["entries"][hit[0]][hit[1]]["fake"]
        if cat == "host":
            site = site_of(real)
            sites = self.d["sites"]
            if site not in sites:
                sites[site] = len(sites) + 1
            role = role_of(real)
            fake = self._next(f"host:{sites[site]}:{role}", lambda n: f"DC{sites[site]}-{role}{n}")
        elif cat == "domain":
            fake = self._next("domain", lambda n: "example.net" if n == 1 else f"dom{n}.example.net")
        elif cat == "term":                                     # one number per term, a letter per case variant
            base = self.d.setdefault("term_base", {})
            low = real.lower()
            if low not in base:
                base[low] = int(self._next("term", lambda n: f"TERM{n:03d}A", substring=True)[4:7])
            variants = sum(1 for e in ents.values() if e["real"].lower() == low)
            fake = f"TERM{base[low]:03d}{chr(ord('A') + variants)}"
        else:
            fake = self._next(cat, FMT[cat].format, substring=(cat == "term"))
        ents[key] = {"real": real, "fake": fake}
        self._index.setdefault(real.lower(), (cat, key))
        return fake

    # -- IPs / MACs / ASNs -------------------------------------------------------------------------
    def v4_prefix(self, real24):
        m = self.d["ipv4"]
        if real24 not in m:
            used = set(m.values())
            for p in V4_POOL:
                if p not in used and p not in self.seen_v4_24:
                    m[real24] = p
                    break
            else:
                raise RuntimeError("public IPv4 pool exhausted (more than 515 distinct public /24s)")
        return m[real24]

    def v4_private16(self, real16):
        m = self.d["ipv4_private"]
        if real16 not in m:
            used = set(m.values())
            for n in range(1, 256):
                p = f"10.{n}"
                if p not in used and p not in self.seen_v4_16:
                    m[real16] = p
                    break
            else:
                raise RuntimeError("private IPv4 pool exhausted")
        return m[real16]

    def v6_prefix(self, real48):
        m = self.d["ipv6"]
        if real48 not in m:
            used = set(m.values())
            n = 0
            while True:
                n += 1
                key = f"20010db8{n:04x}"
                if key not in used and key not in self.seen_v6_48:
                    m[real48] = key
                    break
        return m[real48]

    def mac(self, norm):
        m = self.d["mac"]
        if norm not in m:
            m[norm] = len(m) + 1
        return f"020000{m[norm]:06x}"

    def asn(self, n):
        m = self.d["asn"]
        if str(n) not in m:
            used = set(m.values())
            for a in ASN_POOL:
                if a not in used and not self._in_corpus(str(a)):
                    m[str(n)] = a
                    break
        return m[str(n)]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def unquote(v):
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return '"', v[1:-1]
    return "", v


def is_ip(v):
    try:
        ipaddress.ip_address(v.split("/")[0])
        return True
    except ValueError:
        return False


def eligible(cat, v):
    """Is v worth mapping as an identifier?"""
    if not v or S_OPEN in v or v.startswith("<") or len(v) < 3:
        return False
    lv = v.lower()
    if cat == "serial" and v.isdigit() and len(v) >= 8:
        return True
    if lv in R.STOPWORDS or v.isdigit() or is_ip(v):
        return False
    if cat == "user" and lv in R.GENERIC_USERS:
        return False
    if cat == "host" and (lv in NOT_HOSTS or not re.search(r'[A-Za-z]', v)):
        return False
    if cat == "vlan" and re.fullmatch(r'VLAN\d{4}', v, re.I):
        return False
    return True


def global_eligible(cat, real):
    """Should this real value be replaced everywhere (and checked for leaks)?"""
    if cat in ("banner",):
        return False
    if len(real) < 3 or real.lower() in R.STOPWORDS:
        return False
    if cat in ("desc", "vlan", "location", "contact", "org") and len(real) < 4:
        return False
    return True


IPV4_RE = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)')
IPV6_RE = re.compile(r'(?<![\w:.])((?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})'
                     r'(?![\w:])')
MAC_RE = re.compile(r'(?<![\w:.-])([0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}|'
                    r'[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})'
                    r'(?![\w:-]|\.[0-9A-Fa-f])')
MAC_SKIP = ("000000000000", "ffffffffffff", "01005e", "3333", "00000c07ac", "00005e0001", "00005e0002",
            "0180c2", "01000c", "000000000001")
PRIVATE_V4 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]


def mac_norm(s):
    return re.sub(r'[^0-9a-fA-F]', "", s).lower()


def mac_fmt(norm, like):
    if "." in like:
        out = ".".join(norm[i:i + 4] for i in (0, 4, 8))
    else:
        sep = ":" if ":" in like else "-"
        out = sep.join(norm[i:i + 2] for i in range(0, 12, 2))
    return out.upper() if any(c.isupper() for c in like) else out


def is_public_asn(n):
    return (1 <= n <= 64495 and n != 23456) or (131072 <= n <= 4199999999)


# ---------------------------------------------------------------------------------------------
# Phase A: one file, line by line (blocks, secrets, tokenize, discovery)
# ---------------------------------------------------------------------------------------------
MODES = ("default", "deep")


class Options:
    def __init__(self, level="standard", map_private=False, keep_descriptions=False, keep_nets=(), mode="deep"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode
        self.level, self.map_private, self.keep_descriptions = level, map_private, keep_descriptions
        self.keep_nets = [ipaddress.ip_network(n.strip(), strict=False) for n in keep_nets if n.strip()]


def banner_token(inner, mp, tok):
    """Banner text -> BANNER-NN, keeping the surrounding line breaks/spaces outside the token."""
    core = inner.strip()
    if not core:
        return inner
    a = inner.index(core[0])
    return inner[:a] + tok.add(mp.name("banner", core), "banner") + inner[a + len(core):]


HEX_LINE = re.compile(r'^\s*[0-9A-Fa-f]{8}(?:\s+[0-9A-Fa-f]{1,8})*\s*$')
SECRET_RULES_ORDERED = R.SECRET_RULES[5:] + [
    ("KEY", re.compile(r'(?P<pre>\bauthentication\s+text\s+)' + R._V, re.I)),          # HSRP/VRRP
    ("KEY", re.compile(r'(?P<pre>\bpre-shared-secret\s+)' + R._V, re.I)),
] + R.SECRET_RULES[:5]                       # keyword rules first, then the generic safety nets


def phase_a(text, mp, tok, opt):
    out = []
    lines = text.splitlines(keepends=True)
    top, fstack = "", []
    disc = R.DISCOVERY_RULES + (R.STRICT_DISCOVERY_RULES if opt.level == "strict" else [])
    i, n = 0, len(lines)

    def ctx_ok(ctx):
        if ctx is None:
            return True
        if ctx.startswith("^"):
            return re.search(ctx, top, re.I) is not None
        return bool(fstack) and fstack[-1] == ctx

    while i < n:
        raw = lines[i]
        body = raw.rstrip("\r\n")
        eol = raw[len(body):]
        i += 1

        # ---- PEM blocks (certificates / private keys, any vendor) ----
        mb = R.PEM_BEGIN.search(body)
        if mb:
            prefix, rest_line, j = body[:mb.start()], body, i
            me = R.PEM_END.search(body, mb.end())
            while not me and j < n:
                rest_line = lines[j].rstrip("\r\n")
                eol = lines[j][len(rest_line):]
                me = R.PEM_END.search(rest_line)
                j += 1
            i = j
            suffix = rest_line[me.end():] if me else ""
            out.append(prefix + tok.add("<PEM-REDACTED>", "secret") + suffix + eol)
            continue

        # ---- Cisco certificate hex blocks ----
        if R.CERT_BLOCK_START.match(body) and i < n and HEX_LINE.match(lines[i]):
            out.append(raw)
            indent = re.match(r'^\s*', lines[i]).group(0)
            last_eol = "\n"
            while i < n and not R.CERT_BLOCK_END.match(lines[i]):
                last_eol = lines[i][len(lines[i].rstrip("\r\n")):] or "\n"
                i += 1
            out.append(indent + tok.add("<CERTIFICATE-REDACTED>", "secret") + last_eol)
            continue

        # ---- banners (deep only: banner text is identifying, not secret) ----
        mban = R.BANNER_START.match(body) if opt.mode == "deep" else None
        if mban:
            pre, rest = mban.group("pre"), mban.group("rest")
            r = rest.strip()
            if not r:                                            # EOS: banner text until "EOF"
                content, j = [], i
                while j < n and lines[j].rstrip("\r\n").strip() != "EOF":
                    content.append(lines[j])
                    j += 1
                if j < n:
                    text_b = "".join(content)
                    keep_eol = text_b[len(text_b.rstrip("\r\n")):]
                    out.append(raw)
                    if text_b:
                        out.append(tok.add(mp.name("banner", text_b.rstrip("\r\n")), "banner") + keep_eol)
                    i = j
                    continue
            else:
                lead = rest[:len(rest) - len(rest.lstrip())]
                rest2 = rest.lstrip()
                delim = "^C" if rest2.startswith("^C") else rest2[0]
                if delim == "^C" or not delim.isalnum():
                    after = rest2[len(delim):]
                    k = after.find(delim)
                    if k >= 0:                                   # single-line banner
                        inner = after[:k]
                        tail = after[k + len(delim):]
                        out.append(pre + lead + delim + banner_token(inner, mp, tok) + delim + tail + eol)
                        continue
                    content, j, found = [after + eol], i, False
                    while j < n:
                        lb = lines[j].rstrip("\r\n")
                        k = lb.find(delim)
                        if k >= 0:
                            content.append(lb[:k])
                            tail, eol2 = lb[k + len(delim):], lines[j][len(lb):]
                            found = True
                            j += 1
                            break
                        content.append(lines[j])
                        j += 1
                    if found:
                        inner = "".join(content)
                        out.append(pre + lead + delim + banner_token(inner, mp, tok) + delim + tail + eol2)
                        i = j
                        continue
                else:                                            # ASA style: one banner line of text
                    out.append(pre + lead + tok.add(mp.name("banner", rest2), "banner") + eol)
                    continue

        # ---- block context ----
        mcfg = re.match(r'^\s*config\s+(.+?)\s*$', body)
        if mcfg and not re.match(r'^\s*config\s+(?:t|term|terminal|replace|session)\b', body, re.I):
            fstack.append(mcfg.group(1).strip('"').lower())
        elif re.match(r'^\s*end\s*$', body) and fstack:
            fstack.pop()
        if body and not body[0].isspace() and body.strip() not in ("!", "exit", "end", "#"):
            top = body

        # ---- secrets ----
        def sec_sub(kind):
            def f(m):
                v = m.group("val")
                if S_OPEN in v or v.startswith("<"):
                    return m.group(0)
                q, _ = unquote(v)
                typ = m.groupdict().get("typ")
                ph = tok.add(R.placeholder(kind, typ), "secret")
                tail = m.group(0)[m.end("val") - m.start(0):]
                if q and typ:                                    # key 7 "x" -> key 7 "<KEY type=7>"
                    return m.group("pre") + m.string[m.start("typ"):m.start("val")] + q + ph + q + tail
                return m.group("pre") + q + ph + q + tail
            return f

        for kind, rx in SECRET_RULES_ORDERED:
            body = rx.sub(sec_sub(kind), body)
        if R.BLOCK_KEY_PARENTS.match(top) and top != body:
            body = R.BLOCK_KEY_LINE.sub(sec_sub("KEY"), body)
        if fstack and fstack[-1] == "system snmp community":
            body = R.FORTI_SNMP_NAME.sub(sec_sub("SNMP-COMMUNITY"), body)
        if opt.mode == "default":                        # secrets only: no names, text or addresses
            out.append(body + eol)
            continue

        # ---- tokenize free-text values ----
        for cat, ctx, rx in R.TOKENIZE_RULES:
            if cat == "desc" and opt.keep_descriptions:
                continue
            if not ctx_ok(ctx):
                continue

            def tk(m, cat=cat):
                q, inner = unquote(m.group("val"))
                if not inner.strip() or S_OPEN in inner or (cat == "vlan" and not eligible("vlan", inner)):
                    return m.group(0)
                return (m.group("pre") + q + tok.add(mp.name(cat, inner), cat) + q
                        + (m.group("post") if "post" in m.groupdict() and m.group("post") else ""))
            body = rx.sub(tk, body)

        # ---- discovery (identifiers replaced everywhere later) ----
        for cat, ctx, rx in disc:
            if not ctx_ok(ctx):
                continue
            for m in rx.finditer(body):
                _, v = unquote(m.group("val"))
                if cat == "host" and "." in v and not is_ip(v):
                    head, _, dom = v.partition(".")
                    if re.search(r'\.[A-Za-z]{2,}$', "." + dom) and eligible("domain", dom):
                        mp.name("domain", dom)
                    v = head
                if eligible(cat, v):
                    mp.name(cat, v)
        for m in R.ASN_CONTEXT.finditer(body):
            a = int(m.group("val"))
            if is_public_asn(a):
                mp.asn(a)

        out.append(body + eol)
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# Phase B: global replacement (MAC, IP, names, terms, ASN) then sentinel expansion
# ---------------------------------------------------------------------------------------------
def build_name_regex(mp, reverse=False):
    alts, lookup_cs, lookup_ci = [], {}, {}
    for cat, ents in mp.d["entries"].items():
        if cat == "term":
            continue
        for e in ents.values():
            src, dst = (e["fake"], e["real"]) if reverse else (e["real"], e["fake"])
            if not reverse and not global_eligible(cat, src):
                continue
            ci = reverse or cat in CI_CATS
            (lookup_ci if ci else lookup_cs).setdefault(src.lower() if ci else src, (dst, cat))
            alts.append((src, ci))
    if not alts:
        return None, {}, {}
    alts.sort(key=lambda a: -len(a[0]))
    pat = "|".join(f"(?i:{re.escape(s)})" if ci else re.escape(s) for s, ci in alts)
    return re.compile(r'(?<![A-Za-z0-9])(?:' + pat + r')(?![A-Za-z0-9])'), lookup_cs, lookup_ci


def build_term_regex(mp, reverse=False):
    ents = mp.d["entries"].get("term", {})
    if not ents:
        return None, {}
    lk = {}
    for e in ents.values():
        src, dst = (e["fake"], e["real"]) if reverse else (e["real"], e["fake"])
        lk.setdefault(src.lower(), dst)
    pat = "|".join(re.escape(s) for s in sorted(lk, key=len, reverse=True))
    return re.compile(pat, re.I), lk


def in_keep(ip, opt):
    return any(ip in net for net in opt.keep_nets if net.version == ip.version)


def phase_b(text, mp, tok, opt, name_rx, term_rx):
    if opt.mode == "default":
        return tok.expand(text)
    # MAC
    def mac_sub(m):
        s = m.group(1)
        nm = mac_norm(s)
        if nm.startswith(MAC_SKIP) or nm.startswith("020000"):
            return s
        return tok.add(mac_fmt(mp.mac(nm), s), "mac")
    text = MAC_RE.sub(mac_sub, text)

    # IPv4
    def v4_sub(m):
        s = m.group(1)
        try:
            ip = ipaddress.IPv4Address(s)
        except ValueError:
            return s
        if in_keep(ip, opt):
            return s
        o = s.split(".")
        if ip.is_global and not ip.is_multicast:
            return tok.add(mp.v4_prefix(".".join(o[:3])) + "." + o[3], "ipv4")
        if opt.map_private and any(ip in net for net in PRIVATE_V4):
            return tok.add(mp.v4_private16(".".join(o[:2])) + "." + o[2] + "." + o[3], "ipv4")
        return s
    text = IPV4_RE.sub(v4_sub, text)

    # IPv6
    def v6_sub(m):
        s = m.group(1)
        try:
            ip = ipaddress.IPv6Address(s)
        except ValueError:
            return s
        if not ip.is_global or ip.is_multicast or in_keep(ip, opt):
            return s
        h = ip.exploded.replace(":", "")
        fake = mp.v6_prefix(h[:12]) + h[12:]
        out = str(ipaddress.IPv6Address(":".join(fake[i:i + 4] for i in range(0, 32, 4))))
        return tok.add(out.upper() if any(c.isupper() for c in s) else out, "ipv6")
    text = IPV6_RE.sub(v6_sub, text)

    # names (one pass, longest first)
    rx, lcs, lci = name_rx
    if rx:
        def nm_sub(m):
            s = m.group(0)
            hit = lcs.get(s) or lci.get(s.lower())
            return tok.add(hit[0], hit[1]) if hit else s
        text = rx.sub(nm_sub, text)

    # denylist terms (substring)
    trx, tlk = term_rx
    if trx:                                             # each case variant keeps its own fake (exact restore)
        text = trx.sub(lambda m: tok.add(mp.name("term", m.group(0)), "term"), text)

    # ASNs
    def asn_ctx(m):
        a = int(m.group("val"))
        if not is_public_asn(a):
            return m.group(0)
        return m.group("pre") + tok.add(str(mp.asn(a)), "asn")
    text = R.ASN_CONTEXT.sub(asn_ctx, text)
    if mp.d["asn"]:                                     # bare ASNs only on AS-path lines
        arx = re.compile(r'(?<![\w.:/-])(' + "|".join(sorted(mp.d["asn"], key=len, reverse=True)) + r')(?![\w.:/-])')
        text = "".join(arx.sub(lambda m: tok.add(str(mp.d["asn"][m.group(1)]), "asn"), ln)
                       if R.AS_PATH_LINE.search(ln) else ln for ln in text.splitlines(keepends=True))

    return tok.expand(text)


# ---------------------------------------------------------------------------------------------
# Verify (fail-closed leak check on sanitized text)
# ---------------------------------------------------------------------------------------------
def mask(s):
    s = s.strip()
    return (s[:2] + "***" + f"({len(s)} chars)") if len(s) > 2 else "***"


def verify_text(text, mp, opt, name_rx_real, term_rx_real):
    """Return list of (line_no, kind, masked_value)."""
    found = []
    fake_v4 = set(mp.d["ipv4"].values()) | set(mp.d["ipv4_private"].values())
    fake_rx = build_name_regex(mp, reverse=True)[0]
    fake_trx = build_term_regex(mp, reverse=True)[0]
    for ln, line in enumerate(text.splitlines(), 1):
        if fake_rx:                                     # a real value inside a fake token is not a leak
            line = fake_rx.sub(lambda m: " " * len(m.group(0)), line)
        if fake_trx:
            line = fake_trx.sub(lambda m: " " * len(m.group(0)), line)
        for kind, rx in R.VERIFY_SECRET_PATTERNS:
            for m in rx.finditer(line):
                if kind == "secret keyword with value" and not _real_secret_context(line, m):
                    continue
                found.append((ln, kind, mask(m.group(0))))
        if opt.mode == "default":                        # identifiers and addresses are kept on purpose
            continue
        rx = name_rx_real[0]
        if rx:
            for m in rx.finditer(line):
                found.append((ln, "real identifier", mask(m.group(0))))
        if term_rx_real[0]:
            for m in term_rx_real[0].finditer(line):
                found.append((ln, "denylist term", mask(m.group(0))))
        for m in IPV4_RE.finditer(line):
            try:
                ip = ipaddress.IPv4Address(m.group(1))
            except ValueError:
                continue
            if ip.is_global and not ip.is_multicast and not in_keep(ip, opt) \
                    and ".".join(m.group(1).split(".")[:3]) not in fake_v4:
                found.append((ln, "public IPv4", mask(m.group(1))))
        for m in IPV6_RE.finditer(line):
            try:
                ip = ipaddress.IPv6Address(m.group(1))
            except ValueError:
                continue
            if ip.is_global and not ip.is_multicast and not in_keep(ip, opt):
                found.append((ln, "public IPv6", mask(m.group(1))))
        for m in MAC_RE.finditer(line):
            if mac_norm(m.group(1)) in mp.d["mac"]:
                found.append((ln, "real MAC", mask(m.group(1))))
    return found


PH_RE = re.compile(r'<(?:PASSWORD|KEY|PSK|SNMP-COMMUNITY|TOKEN)\b')


def _real_secret_context(line, m):
    s = m.group(0).lower()
    if s.startswith("community") and "snmp" not in line.lower():   # only SNMP communities are secrets
        return False
    return not PH_RE.search(line, m.start())         # value already replaced further along the line


# ---------------------------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------------------------
def restore_text(text, mp):
    tok = Tokens()
    rv4 = {v: k for k, v in mp.d["ipv4"].items()}
    rv4p = {v: k for k, v in mp.d["ipv4_private"].items()}
    rv6 = {v: k for k, v in mp.d["ipv6"].items()}
    rmac = {v: k for k, v in mp.d["mac"].items()}
    rasn = {str(v): k for k, v in mp.d["asn"].items()}

    def mac_sub(m):
        s = m.group(1)
        nm = mac_norm(s)
        if nm.startswith("020000") and int(nm[6:], 16) in rmac:
            return tok.add(mac_fmt(rmac[int(nm[6:], 16)], s), "mac")
        return s
    text = MAC_RE.sub(mac_sub, text)

    def v4_sub(m):
        s = m.group(1)
        o = s.split(".")
        p3, p2 = ".".join(o[:3]), ".".join(o[:2])
        if p3 in rv4:
            return tok.add(rv4[p3] + "." + o[3], "ipv4")
        if p2 in rv4p:
            return tok.add(rv4p[p2] + "." + o[2] + "." + o[3], "ipv4")
        return s
    text = IPV4_RE.sub(v4_sub, text)

    def v6_sub(m):
        s = m.group(1)
        try:
            ip = ipaddress.IPv6Address(s)
        except ValueError:
            return s
        h = ip.exploded.replace(":", "")
        if h[:12] in rv6:
            real = rv6[h[:12]] + h[12:]
            return tok.add(str(ipaddress.IPv6Address(":".join(real[i:i + 4] for i in range(0, 32, 4)))), "ipv6")
        return s
    text = IPV6_RE.sub(v6_sub, text)

    if rasn:
        arx = re.compile(r'(?<![\w.:/-])(' + "|".join(sorted(rasn, key=len, reverse=True)) + r')(?![\w.:/-])')
        text = arx.sub(lambda m: tok.add(rasn[m.group(1)], "asn"), text)

    rx, _, lci = build_name_regex(mp, reverse=True)
    trx, tlk = build_term_regex(mp, reverse=True)
    if rx or trx:
        parts = []
        if rx:
            parts.append(rx.pattern)
        if trx:
            parts.append("(?i:" + trx.pattern + ")")
        crx = re.compile("|".join(parts))

        def sub(m):
            s = m.group(0)
            if s.lower() in tlk:
                return tok.add(tlk[s.lower()], "term")
            hit = lci.get(s.lower())
            return tok.add(hit[0], hit[1]) if hit else s
        text = crx.sub(sub, text)
    return tok.expand(text)



def read_files(src):
    files = []
    if os.path.isfile(src):
        return [(os.path.basename(src), src)]
    for root, _, names in os.walk(src):
        for nm in sorted(names):
            p = os.path.join(root, nm)
            files.append((os.path.relpath(p, src), p))
    return sorted(files)


def load_text(p):
    with open(p, "rb") as fh:
        data = fh.read()
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="surrogateescape")


def write_text(p, text):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(text.encode("utf-8", errors="surrogateescape"))


def load_terms(paths):
    terms = []
    for p in paths:
        try:
            for line in open(p, encoding="utf-8"):
                t = line.split("#")[0].strip()
                if len(t) >= 3:
                    terms.append(t)
        except OSError:
            pass
    return terms


MODEL_NOTE = """NOTE FOR THE AI MODEL — these files were sanitized before upload.
- Device names (DC1-SW1, DC2-R1, DC1-FW1, DC3-NET1), users (USER01), AAA groups (AAA-GRP01),
  descriptions (DESC-0001), VLAN names (VLAN-NAME-001), objects (OBJ-0001), terms (TERM001A),
  domains (example.net), IPs in 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 198.18.0.0/15 and
  2001:db8::/32, MACs starting 02:00:00, and AS numbers 64496-64511 / 65536+ are consistent
  pseudonyms. The same token always means the same real object across all files.
- Placeholders like <PASSWORD type=7>, <KEY>, <PSK>, <SNMP-COMMUNITY> replace removed secrets.
  Do not invent values for them; refer to them by placeholder.
- In your answer, write every token EXACTLY as it appears (same spelling, no reformatting),
  so it can be mapped back to the real names.
"""
