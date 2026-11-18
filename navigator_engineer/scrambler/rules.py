"""rules.py — what LOG-SANITIZER looks for, per vendor.

Vendors covered: Cisco IOS / IOS-XE / NX-OS / Viptela (vEdge, cEdge) / ASA / Firepower (FTD),
Arista EOS, Palo Alto PAN-OS (set and XML format), Fortinet FortiOS.

Three kinds of rules:
  SECRET_RULES      value is destroyed -> <PASSWORD type=N> style placeholder (never stored, never restored)
  TOKENIZE_RULES    whole free-text value -> reversible token (DESC-0001, VLAN-NAME-001, ...)
  DISCOVERY_RULES   identifier is collected, then replaced EVERYWHERE in every file (hosts, users, AAA groups...)

Each rule regex runs on ONE line (no line ending). Group names:
  pre  = text kept before the value, val = the value, post = text kept after (optional)
"""
import re

I = re.IGNORECASE

# ---------------------------------------------------------------------------------------------
# Words never treated as identifiers (keeps keywords like "default", "inside", "tacacs" intact).
# ---------------------------------------------------------------------------------------------
STOPWORDS = {
    "default", "management", "mgmt", "inside", "outside", "dmz", "any", "all", "global", "internal",
    "external", "trust", "untrust", "local", "tacacs", "tacacs+", "radius", "ldap", "none", "permit",
    "deny", "enable", "disable", "true", "false", "yes", "no", "on", "off", "shared", "vsys1", "root",
    "public", "private", "test", "lab", "core", "uplink", "downlink", "trunk", "access", "native",
    "transport", "service", "services", "users", "user", "guest", "voice", "data", "vlan", "admin",
    "ssl.root", "dfltgrppolicy", "defaultl2lgroup", "defaultragroup", "defaultwebvpngroup",
    "fddi-default", "token-ring-default", "fddinet-default", "trnet-default", "unused", "reserved",
    "loopback", "tunnel", "null", "management-vrf", "mgmt-vrf", "inet", "inet6", "ipv4", "ipv6",
}

# Usernames that are generic and safe to leave as-is (user decision 2026-10-12).
GENERIC_USERS = {
    "admin", "administrator", "root", "cisco", "engineer", "operator", "netops", "netadmin", "monitor",
    "readonly", "read-only", "guest", "network-admin", "network-operator", "superuser", "super_user",
    "enable", "nobody", "maintenance", "support", "audit", "auditor", "readwrite", "rw", "ro",
    "basic", "netadmin", "operator", "tacacs", "radius", "local", "system", "daemon", "api", "automation",
}

# Lowercase-hex placeholder text for secrets. {kind} e.g. PASSWORD, SNMP-COMMUNITY, PSK, KEY.
def placeholder(kind, typ=None):
    return f"<{kind} type={typ}>" if typ not in (None, "") else f"<{kind}>"


# ---------------------------------------------------------------------------------------------
# SECRET RULES  (kind, regex). Optional group "typ" = Cisco/Arista encryption type digit.
# ---------------------------------------------------------------------------------------------
_V = r'(?P<val>"[^"]*"|\S+)'          # value: quoted string or one token
_T = r'(?:(?P<typ>\d{1,2})\s+)?'      # optional type number

_NOT_SECRET_AFTER_PASSWORD = (r'(?!(?:encryption|recovery|strength-check|strength|policy|secure-mode|'
                              r'prompt|chang\w*|expir\w*|history|lifetime|max-\w+|min-\w+|length|complexity|'
                              r'reuse|management|hash|aaa|retry|aging|mode|ENC\b)\b)')

SECRET_RULES = [
    # ---- generic crypted / encrypted blobs anywhere on the line (safety net, run first) ----
    ("PASSWORD", re.compile(r'(?P<pre>)(?P<val>\$(?:1|5|6|8|9|14|sha512)\$[^\s"\'<]+)')),
    ("PASSWORD", re.compile(r'(?P<pre>\bENC\s+)(?P<val>[A-Za-z0-9+/=]{8,})')),           # FortiOS
    ("PASSWORD", re.compile(r'(?P<pre>)(?P<val>-AQ==[A-Za-z0-9+/=]+)')),                   # PAN-OS
    ("KEY",      re.compile(r'(?P<pre>)(?P<val>\b0x[0-9a-fA-F]{16,})')),                   # NX-OS localized keys
    ("KEY",      re.compile(r'(?P<pre>-cvauth=(?:key|token),)(?P<val>[^\s,]+)')),         # Arista TerminAttr
    # ---- Cisco / Arista / NX-OS / ASA / Viptela keyword forms ----
    ("PASSWORD", re.compile(r'(?P<pre>\b(?:secret|password|passwd)\s+)' + _NOT_SECRET_AFTER_PASSWORD
                            + r'(?:(?P<typ>\d{1,2}|sha512|pbkdf2|md5)\s+)?' + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bkey-string\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bauthentication-key\s+(?:\d+\s+md5\s+)?)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bmessage-digest-key\s+\d+\s+md5\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bntp authentication-key\s+\d+\s+\S+\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>^\s*(?:tacacs-server|radius-server)\b.*?\bkey\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>^\s*(?:tacacs|radius)\s+server\b.*?\bkey\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>^\s*server-private\s+\S+.*?\bkey\s+)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\b(?:secret-key|shared-secret|pairwise-key|auth-key|server-key|'
                            r'encryption-key|api-key|apikey)\s+)' + _T + _V, I)),
    ("PSK",      re.compile(r'(?P<pre>\bisakmp key\s+)' + _T + _V, I)),
    ("PSK",      re.compile(r'(?P<pre>\bpre-shared-key\s+(?:local\s+|remote\s+|key\s+|hex\s+|'
                            r'address\s+\S+(?:\s+\d+\.\d+\.\d+\.\d+)?\s+key\s+|hostname\s+\S+\s+key\s+)?)' + _T + _V, I)),
    ("PSK",      re.compile(r'(?P<pre>\bpre-share\s+key\s+)' + _T + _V, I)),
    ("PSK",      re.compile(r'(?P<pre>\b(?:wpa-psk|psk)\s+(?:ascii\s+|hex\s+)?)' + _T + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bfailover key\s+(?:hex\s+)?)' + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bkey config-key password-encrypt\s+)' + _V, I)),
    ("PASSWORD", re.compile(r'(?P<pre>\bldap-login-password\s+)' + _V, I)),
    ("TOKEN",    re.compile(r'(?P<pre>\b(?:idtoken|id-token|auth-code|authcode|license-key|'
                            r'registration-key)\s+)(?!(?:enable|disable|interval|timeout)\b)' + _V, I)),
    # ---- SNMP ----
    ("SNMP-COMMUNITY", re.compile(r'(?P<pre>^\s*snmp-server\s+community\s+)' + _T + _V, I)),
    ("SNMP-COMMUNITY", re.compile(r'(?P<pre>^\s*snmp-server\s+host\s+.*?\bcommunity\s+)' + _T + _V, I)),
    ("SNMP-COMMUNITY", re.compile(r'(?P<pre>^\s*snmp-server\s+host\s+(?:\S+\s+)?\d[\d.:a-fA-F]*\s+'
                                  r'(?:vrf\s+\S+\s+)?(?:(?:informs|traps)\s+)?(?:version\s+(?:1|2c)\s+)?)'
                                  r'(?!(?:version|informs|traps|vrf|use-vrf|udp-port|source-interface|community)\b)'
                                  + _V, I)),
    ("SNMP-COMMUNITY", re.compile(r'(?P<pre>\bcommunity-string\s+)' + _V, I)),            # PAN-OS
    ("KEY",      re.compile(r'(?P<pre>\bauth\s+(?:md5|sha|sha-?\d+|sha2-\d+)\s+)' + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\bpriv\s+(?:(?:aes|des|3des|aes-?\d+)(?:\s+\d{3})?\s+)?)'
                            r'(?!(?:aes|des|3des|read|write|notify|access|context|match)\b)' + _V, I)),
    ("KEY",      re.compile(r'(?P<pre>\b(?:auth|priv)-password\s+)' + _V, I)),            # PAN-OS
    # ---- FortiOS "set <secret-ish> value" ----
    ("PASSWORD", re.compile(r'(?P<pre>^\s*set\s+[\w-]*(?:password|passwd|secret|passphrase|pwd|psksecret|'
                            r'(?<![\w])key|-key)\s+(?:ENC\s+)?)(?P<val>.+?)\s*$', I)),
    # ---- PAN-OS set format ----
    ("PASSWORD", re.compile(r'(?P<pre>\b(?:phash|passphrase)\s+)' + _V, I)),
    # ---- XML (PAN-OS) ----
    ("PASSWORD", re.compile(r'(?P<pre><(?P<tag>phash|password|secret|key|community-string|authentication-key|'
                            r'auth-password|priv-password|passphrase|pre-shared-key|api-key)>)(?P<val>[^<]+)', I)),
]

# Line-level secret rules that need block context (applied by the engine):
#   Cisco named tacacs/radius server, ASA aaa-server host block:   " key <x>"
#   FortiOS "config system snmp community" -> "set name <community>"
BLOCK_KEY_LINE = re.compile(r'(?P<pre>^\s+key\s+)' + _T + _V + r'\s*$', I)
BLOCK_KEY_PARENTS = re.compile(r'^\s*(?:tacacs\s+server|radius\s+server|aaa-server\s+\S+\s+\(|aaa-server\s+\S+\s+host|'
                               r'tacacs-server\s+host|radius-server\s+host|aaa\s+server\s+radius)', I)

# Multi-line blocks removed whole -> single placeholder line.
PEM_BEGIN = re.compile(r'-----BEGIN [A-Z0-9 ]+-----')
PEM_END = re.compile(r'-----END [A-Z0-9 ]+-----')
CERT_BLOCK_START = re.compile(r'^\s*certificate\s+(?:self-signed\s+|ca\s+|ra-sign\s+|ra-encrypt\s+)?[0-9A-Fa-f]+\s*$', I)
CERT_BLOCK_END = re.compile(r'^\s*quit\s*$', I)
BANNER_START = re.compile(r'^(?P<pre>\s*banner\s+(?:motd|login|exec|incoming|slip-ppp|prompt-timeout|asdm|'
                          r'config-save)\s*)(?P<rest>.*)$', I)


# ---------------------------------------------------------------------------------------------
# TOKENIZE RULES (category, regex): whole value -> reversible token. Optional "ctx" = only when the
# current block header (last top-level line, or FortiOS "config ..." path) matches.
# ---------------------------------------------------------------------------------------------
_REST = r'(?P<val>"[^"]*"|\S(?:.*\S)?)(?P<post>\s*)$'
TOKENIZE_RULES = [
    ("desc",     None, re.compile(r'(?P<pre>^\s*description\s+)' + _REST, I)),
    ("desc",     None, re.compile(r'(?P<pre>^\s*Description:\s*)' + _REST)),
    ("desc",     None, re.compile(r'(?P<pre>^\s*(?:ip\s+)?access-list\s+\S+\s+remark\s+)' + _REST, I)),
    ("desc",     None, re.compile(r'(?P<pre>^\s*remark\s+)' + _REST, I)),
    ("desc",     None, re.compile(r'(?P<pre>^\s*\d+\s+remark\s+)' + _REST, I)),
    ("desc",     None, re.compile(r'(?P<pre>^\s*set\s+(?:description|comments?)\s+)' + _REST, I)),       # FortiOS
    ("desc",     "system interface", re.compile(r'(?P<pre>^\s*set\s+alias\s+)' + _REST, I)),            # FortiOS
    ("desc",     None, re.compile(r'(?P<pre>^set\s.*?\s(?:description|comment)\s+)(?P<val>"[^"]*"|\S+)(?P<post>)', I)),  # PAN
    ("banner",   None, re.compile(r'(?P<pre>\blogin-banner\s+)(?P<val>"[^"]*"|\S+)(?P<post>)', I)),                   # PAN
    ("desc",     None, re.compile(r'(?P<pre><description>)(?P<val>[^<]+)(?P<post>)', I)),                  # PAN XML
    ("vlan",     r"^vlan\s+\d", re.compile(r'(?P<pre>^\s+name\s+)' + _REST, I)),
    ("vlan",     None, re.compile(r'(?P<pre>^\d{1,4}\s+)(?P<val>\S+)(?P<post>\s+(?:active|act/lshut|act/unsup|'
                                  r'act/ishut|suspended|sus/lshut|suspend)\b.*)$', I)),                     # show vlan
    ("location", None, re.compile(r'(?P<pre>^\s*snmp-server\s+location\s+)' + _REST, I)),
    ("contact",  None, re.compile(r'(?P<pre>^\s*snmp-server\s+contact\s+)' + _REST, I)),
    ("location", "system snmp sysinfo", re.compile(r'(?P<pre>^\s*set\s+location\s+)' + _REST, I)),
    ("contact",  "system snmp sysinfo", re.compile(r'(?P<pre>^\s*set\s+contact-info\s+)' + _REST, I)),
    ("location", None, re.compile(r'(?P<pre>\bsnmp-system\s+location\s+)(?P<val>"[^"]*"|\S+)(?P<post>)', I)),
    ("contact",  None, re.compile(r'(?P<pre>\bsnmp-system\s+contact\s+)(?P<val>"[^"]*"|\S+)(?P<post>)', I)),
    ("location", r"^system\b", re.compile(r'(?P<pre>^\s+location\s+)' + _REST, I)),                       # Viptela
    ("org",      None, re.compile(r'(?P<pre>^\s*(?:sp-)?organization-name\s+)' + _REST, I)),
    ("org",      None, re.compile(r'(?P<pre>\bsubject-name\s+)' + _REST, I)),
]


# ---------------------------------------------------------------------------------------------
# DISCOVERY RULES (category, ctx, regex with group "val"). Values found here are replaced
# everywhere (all files) by the engine. ctx as in TOKENIZE_RULES.
# ---------------------------------------------------------------------------------------------
_N = r'"?(?P<val>[^"\s]+)"?'
_CMD = (r'(?:sh|sho|show|conf|config|configure|dir|ping|traceroute|terminal|term|display|exit|end|copy|'
        r'write|wr|run|bash|diagnose|diag|get|execute|exec|request|debug|clear|reload|test|tail|less)\b')
DISCOVERY_RULES = [
    # ---- hostnames ----
    ("host", None, re.compile(r'^\s*(?:hostname|switchname|host-name)\s+' + _N + r'\s*$', I)),
    ("host", "system global", re.compile(r'^\s*set\s+(?:hostname|alias)\s+' + _N, I)),                    # FortiOS
    ("host", None, re.compile(r'\bset\s+deviceconfig\s+system\s+hostname\s+' + _N, I)),                    # PAN
    ("host", None, re.compile(r'<hostname>(?P<val>[^<\s]+)</hostname>', I)),
    ("host", None, re.compile(r'^(?P<val>[A-Za-z][\w.-]{2,62})(?:\([\w/:.-]*\))?[#>]\s*' + _CMD, I)),      # CLI prompt
    ("host", None, re.compile(r'^(?P<val>[A-Za-z][\w.-]{2,62})(?:\s\([\w-]+\))?\s[#$]\s+' + _CMD, I)),     # FortiOS prompt
    ("host", None, re.compile(r'^[\w.-]+@(?P<val>[A-Za-z][\w.-]{2,62}?)(?:\([\w-]+\))?[#>]', I)),          # PAN prompt
    ("host", None, re.compile(r'^\s*(?:Device ID|System Name|SysName|Neighbor Device ID|Remote system name)'
                              r'\s*:\s*(?P<val>[^\s(]+)', I)),
    ("host", None, re.compile(r'^(?:<\d+>\d*\s*)?(?:\d{4}\s+)?(?:[A-Z][a-z]{2}\s+\d{1,2}\s+(?:\d{4}\s+)?\d{2}:\d{2}:\d{2}(?:\.\d+)?'
                              r'(?:\s+[A-Z]{2,5})?|\d{4}-\d{2}-\d{2}T\S+)\s+(?P<val>[A-Za-z][\w.-]{2,62})\s+'
                              r'(?:%|:\s*%|\S+?\[\d+\]:|[A-Za-z][\w-]*:)')),                                  # syslog header
    ("host", None, re.compile(r'\bdevname="?(?P<val>[^"\s]+)"?', I)),                                     # FortiOS log
    ("host", None, re.compile(r'\bvbond\s+(?P<val>[A-Za-z][\w.-]+\.[A-Za-z]{2,})', I)),                   # Viptela FQDN
    # ---- users ----
    ("user", None, re.compile(r'^\s*username\s+' + _N, I)),
    ("user", r"^system\b|^\s*aaa\b", re.compile(r'^\s*user\s+' + _N + r'\s*$', I)),                      # Viptela
    ("user", None, re.compile(r'\bset\s+mgt-config\s+users\s+' + _N, I)),                                 # PAN
    ("user", None, re.compile(r'^[\s]*(?P<val>[\w.-]{2,})@[A-Za-z][\w.-]+(?:\([\w-]+\))?[#>]')),          # PAN prompt
    ("user", None, re.compile(r'\[user:\s*(?P<val>[^\]\s]+)\]', I)),
    ("user", None, re.compile(r'\bUser:\s*(?P<val>[\w.@\\-]+)')),
    ("user", None, re.compile(r'\buser(?:name)?\s*=\s*"?(?P<val>[\w.@\\-]+)"?', I)),
    ("user", None, re.compile(r"\bUser\s+'(?P<val>[^']+)'", I)),
    ("user", None, re.compile(r'\bby\s+(?P<val>[\w.@\\-]+)\s+on\s+(?:vty|console|tty|ssh)', I)),
    ("user", None, re.compile(r"\buser\s+'?(?P<val>[\w.@\\-]+)'?\s+(?:logged|login|authenticated|has|failed)", I)),
    ("user", "system admin", re.compile(r'^\s*edit\s+' + _N, I)),                                         # FortiOS
    ("user", "user local", re.compile(r'^\s*edit\s+' + _N, I)),                                           # FortiOS
    # ---- AAA groups / server profiles ----
    ("aaa", None, re.compile(r'^\s*aaa\s+group\s+server\s+(?:tacacs\+?|radius|ldap)\s+' + _N, I)),
    ("aaa", None, re.compile(r'^\s*aaa-server\s+(?P<val>\S+)\s+(?:protocol|\(|host)', I)),                 # ASA
    ("aaa", None, re.compile(r'^\s*(?:tacacs|radius)\s+server\s+' + _N, I)),
    ("aaa", None, re.compile(r'\bserver-profile\s+(?:tacplus|radius|ldap|kerberos|saml-idp)\s+' + _N, I)),  # PAN
    ("aaa", None, re.compile(r'\bauthentication-profile\s+' + _N, I)),                                    # PAN
    ("aaa", "user tacacs+", re.compile(r'^\s*edit\s+' + _N, I)),                                          # FortiOS
    ("aaa", "user radius", re.compile(r'^\s*edit\s+' + _N, I)),
    ("aaa", "user ldap", re.compile(r'^\s*edit\s+' + _N, I)),
    ("aaa", "user group", re.compile(r'^\s*edit\s+' + _N, I)),
    # ---- domains ----
    ("domain", None, re.compile(r'^\s*ip\s+domain[- ](?:name|list)\s+(?:vrf\s+\S+\s+)?' + _N, I)),
    ("domain", None, re.compile(r'^\s*(?:dns\s+domain|domain-name|domain-list)\s+' + _N, I)),
    ("domain", None, re.compile(r'\bset\s+deviceconfig\s+system\s+domain\s+' + _N, I)),
    ("domain", "system dns", re.compile(r'^\s*set\s+domain\s+' + _N, I)),
    ("domain", None, re.compile(r'<domain>(?P<val>[^<\s]+)</domain>', I)),
    # ---- emails ----
    ("email", None, re.compile(r'(?P<val>\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b)')),
    # ---- ASA tunnel-groups / group-policies (often customer names) ----
    ("tunnel", None, re.compile(r'^\s*tunnel-group\s+(?P<val>\S+)\s+(?:type|general-attributes|ipsec-attributes|'
                                r'webvpn-attributes|ppp-attributes)', I)),
    ("policy", None, re.compile(r'^\s*group-policy\s+(?P<val>\S+)\s+(?:internal|external|attributes)', I)),
    # ---- serial numbers / device IDs ----
    ("serial", None, re.compile(r'\b(?:Processor board ID|System serial number|Chassis serial number|Serial Number|'
                                r'Serial Num|Serial No\.?|serial-number|serial|SN|chassis-number|devid)'
                                r'(?:\s*[:=]\s*|\s+)"?(?P<val>(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,})"?', I)),
    ("serial", None, re.compile(r'<serial>(?P<val>[^<\s]+)</serial>', I)),
    ("serial", None, re.compile(r'\b(?P<val>F[GSWAPMK][A-Z0-9]{3}[A-Z0-9]{9,11})\b')),                   # Fortinet SN
    ("serial", None, re.compile(r'\blicense udi pid \S+ sn (?P<val>\S+)', I)),
]

# Extra discovery used only with --level strict (object / policy / VRF names).
STRICT_DISCOVERY_RULES = [
    ("object", None, re.compile(r'^\s*object(?:-group)?\s+(?:network|service|protocol|icmp-type|user|security)\s+'
                                + _N, I)),
    ("object", None, re.compile(r'^\s*name\s+\d+\.\d+\.\d+\.\d+\s+(?P<val>\S+)', I)),                    # ASA names
    ("object", None, re.compile(r'^\s*access-list\s+(?P<val>[A-Za-z]\S*)\s+(?:extended|standard|remark|webtype|'
                                r'ethertype|line)', I)),
    ("object", None, re.compile(r'^\s*(?:ip|ipv6|mac)\s+access-list\s+(?:extended\s+|standard\s+)?(?P<val>[A-Za-z]\S*)', I)),
    ("object", None, re.compile(r'^\s*(?:ip\s+|ipv6\s+)?prefix-list\s+(?P<val>[A-Za-z]\S*)', I)),
    ("object", None, re.compile(r'^\s*route-map\s+(?P<val>\S+)', I)),
    ("object", None, re.compile(r'^\s*(?:vrf\s+(?:definition|context|instance)|ip\s+vrf)\s+(?P<val>\S+)', I)),
    ("object", None, re.compile(r'^\s*zone\s+security\s+(?P<val>\S+)', I)),
    ("object", None, re.compile(r'^\s*(?:class-map|policy-map)\s+(?:type\s+\S+\s+)?(?:match-(?:any|all)\s+)?'
                                r'(?P<val>\S+)', I)),
    ("object", None, re.compile(r'^\s*crypto\s+map\s+(?P<val>\S+)', I)),
    ("object", None, re.compile(r'^set\s+(?:shared\s+|vsys\s+\S+\s+|device-group\s+\S+\s+)?'
                                r'(?:(?:pre-|post-)?rulebase\s+\S+\s+)?'
                                r'(?:rules|address|address-group|service|service-group|tag|zone|application-group|'
                                r'profile-group|external-list|profiles\s+\S+)\s+(?P<val>"[^"]+"|\S+)', I)),   # PAN
    ("object", None, re.compile(r'<entry name="(?P<val>[^"]+)"', I)),                                     # PAN XML
    ("object", "firewall address", re.compile(r'^\s*edit\s+' + _N, I)),                                  # FortiOS
    ("object", "firewall addrgrp", re.compile(r'^\s*edit\s+' + _N, I)),
    ("object", "firewall service custom", re.compile(r'^\s*edit\s+' + _N, I)),
    ("object", "firewall service group", re.compile(r'^\s*edit\s+' + _N, I)),
    ("object", "firewall vip", re.compile(r'^\s*edit\s+' + _N, I)),
    ("object", "firewall policy", re.compile(r'^\s*set\s+name\s+' + _N, I)),
    ("object", "vpn ipsec phase1-interface", re.compile(r'^\s*edit\s+' + _N, I)),
    ("object", "vpn ipsec phase2-interface", re.compile(r'^\s*edit\s+' + _N, I)),
]

# FortiOS: "config system snmp community" -> "set name <community>" is a secret.
FORTI_SNMP_NAME = re.compile(r'(?P<pre>^\s*set\s+name\s+)(?P<val>.+?)\s*$', I)

# ---------------------------------------------------------------------------------------------
# BGP AS numbers (public ones are mapped into documentation / reserved ranges).
# ---------------------------------------------------------------------------------------------
ASN_CONTEXT = re.compile(r'(?P<pre>(?:\b(?i:router\s+bgp|remote-as|local-as|peer-as|bgp\s+as|set\s+as|'
                         r'set\s+remote-as|set\s+local-as|as-number)\s*[:=]?\s*)|(?:\bASN?\s*[:=]?\s*))'
                         r'(?P<val>\d{1,10})\b')
# Lines that carry an AS path (show ip bgp, show bgp ... detail): mapped ASNs replaced as bare numbers here.
AS_PATH_LINE = re.compile(r'(?:\d+\s+)+[ie?]\s*$|AS[ -]?path|as-path|Path:', I)

# ---------------------------------------------------------------------------------------------
# VERIFY: patterns that must NOT survive in sanitized output (fail-closed).
# ---------------------------------------------------------------------------------------------
VERIFY_SECRET_PATTERNS = [
    ("crypt hash", re.compile(r'\$(?:1|5|6|8|9|14|sha512)\$[^\s"<]{4,}')),
    ("FortiOS ENC blob", re.compile(r'\bENC\s+[A-Za-z0-9+/=]{8,}')),
    ("PAN-OS -AQ== blob", re.compile(r'-AQ==[A-Za-z0-9+/=]{4,}')),
    ("hex key", re.compile(r'\b0x[0-9a-fA-F]{16,}')),
    ("PEM block", re.compile(r'-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)')),
    ("type-7 string", re.compile(r'\b(?:password|key|key-string|secret)\s+7\s+(?!<)[0-9A-Fa-f]{4,}', I)),
    ("secret keyword with value", re.compile(
        r'\b(?:secret|password|passwd|key-string|psksecret|pre-shared-key|community|phash|passphrase|'
        r'authentication-key|secret-key|shared-secret)\s+(?:\d{1,2}\s+)?(?!<|\x00|' +
        _NOT_SECRET_AFTER_PASSWORD[3:-1] + r'|ENC\s*<)(?=\S)(?!(?:md5|sha|hmac)\b)'
        r'(?!(?:required|optional|none|enable|disable|set|unset|interval|timeout|type|level|attributes|'
        r'group|change|policy|encrypted|hidden|any|all|list|view|access|ro|rw|key|string|file|\d+$)\b)\S+', I)),
]
