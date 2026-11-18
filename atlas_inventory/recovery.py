"""The recovery key: what the user keeps so the CREDENTIALS key, and every backup, can be opened again.

- The CREDENTIALS key itself stays a random 32-byte file (vault.py), so atlas-link runs unattended.
- `init` makes a **recovery key** in the BitLocker style (Microsoft): 48 digits in 8 groups of 6, each group a
  multiple of 11 (a typo in a group is caught), 128 random bits. It is shown once; NAVIGATOR keeps only:
    recovery.json (mode 600, next to the CREDENTIALS key):
      PUBLIC        an X25519 public key (backups and the key escrow are sealed to it; no secret needed)
      PRIVATE       the matching private key, encrypted with scrypt(recovery key) + AES-256-GCM
      ESCROW        the CREDENTIALS key sealed to PUBLIC (refreshed by rekey)
- Opening anything sealed needs the private key, so the recovery key. `check` proves the user has the right
  one: it opens PRIVATE and the ESCROW, and the escrowed key must equal the CREDENTIALS key in use.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

VERSION = 1
GROUPS, MAX_GROUP = 8, 65536
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
SEAL_INFO = b"navigator-seal-v1"


class RecoveryError(Exception):
    """Messages never contain a key."""


def _b(x: bytes) -> str:
    return base64.b64encode(x).decode()


def _u(s: str) -> bytes:
    return base64.b64decode(s)


# ------------------------------------------------------------------ the recovery key (48 digits)
def new_recovery_key() -> str:
    return "-".join(f"{secrets.randbelow(MAX_GROUP) * 11:06d}" for _ in range(GROUPS))


def parse(text: str) -> bytes:
    """The 16 secret bytes of a recovery key typed with or without dashes/spaces."""
    digits = re.sub(r"[\s-]", "", str(text or ""))
    if not re.fullmatch(r"\d{48}", digits):
        raise RecoveryError("a recovery key is 48 digits (8 groups of 6)")
    out = b""
    for i in range(GROUPS):
        g = int(digits[i * 6:(i + 1) * 6])
        if g % 11 or g // 11 >= MAX_GROUP:
            raise RecoveryError(f"group {i + 1} of the recovery key is mistyped")
        out += (g // 11).to_bytes(2, "big")
    return out


def _kek(secret: bytes, salt: bytes, params: dict) -> bytes:
    return Scrypt(salt=salt, length=32, n=params["n"], r=params["r"], p=params["p"]).derive(secret)


# ------------------------------------------------------------------ sealing to the public key
def seal(public_b64: str, data: bytes, aad: bytes = b"") -> dict:
    pub = X25519PublicKey.from_public_bytes(_u(public_b64))
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    k = HKDF(hashes.SHA256(), 32, None, SEAL_INFO + eph_pub + _u(public_b64)).derive(eph.exchange(pub))
    nonce = os.urandom(12)
    return {"EPH": _b(eph_pub), "NONCE": _b(nonce), "CT": _b(AESGCM(k).encrypt(nonce, data, SEAL_INFO + aad))}


def unseal(private: X25519PrivateKey, sealed: dict, aad: bytes = b"") -> bytes:
    pub = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    eph = _u(sealed["EPH"])
    k = HKDF(hashes.SHA256(), 32, None, SEAL_INFO + eph + pub).derive(
        private.exchange(X25519PublicKey.from_public_bytes(eph)))
    try:
        return AESGCM(k).decrypt(_u(sealed["NONCE"]), _u(sealed["CT"]), SEAL_INFO + aad)
    except InvalidTag:
        raise RecoveryError("sealed data does not open with this recovery key") from None


# ------------------------------------------------------------------ recovery.json
def path_for(cfg) -> Path:
    return cfg.key_file.parent / "recovery.json"


def _write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, p)


def load_doc(cfg) -> dict | None:
    p = path_for(cfg)
    return json.loads(p.read_text()) if p.is_file() else None


def make_doc(recovery_key: str) -> dict:
    """PUBLIC + PRIVATE (encrypted with the recovery key); no escrow yet."""
    secret = parse(recovery_key)
    priv = X25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kek(secret, salt, SCRYPT)).encrypt(nonce, raw, b"navigator-recovery-v1")
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"VERSION": VERSION, "CREATED": time.strftime("%Y-%m-%d %H:%M:%S"), "PUBLIC": _b(pub),
            "PRIVATE": {"KDF": "scrypt", **{k.upper(): v for k, v in SCRYPT.items()}, "SALT": _b(salt),
                        "NONCE": _b(nonce), "CT": _b(ct)}}


def unlock(doc: dict, recovery_key: str) -> X25519PrivateKey:
    secret = parse(recovery_key)
    pv = doc["PRIVATE"]
    kek = _kek(secret, _u(pv["SALT"]), {"n": pv["N"], "r": pv["R"], "p": pv["P"]})
    try:
        raw = AESGCM(kek).decrypt(_u(pv["NONCE"]), _u(pv["CT"]), b"navigator-recovery-v1")
    except InvalidTag:
        raise RecoveryError("that is not the recovery key of this NAVIGATOR") from None
    return X25519PrivateKey.from_private_bytes(raw)


def init(cfg, replace: bool = False) -> str:
    """Make the recovery key (and the CREDENTIALS key if there is none). Returns the recovery key to show once."""
    from . import vault
    p = path_for(cfg)
    if p.exists() and not replace:
        raise RecoveryError(f"{p} exists: there is a recovery key already (--replace makes a new one; "
                            "backups made with the old one still need the old one)")
    if not any(k.exists() for k in vault.key_parts(cfg)):
        vault.create_key(cfg)
    key = vault.load_key(cfg)
    rk = new_recovery_key()
    doc = make_doc(rk)
    doc["ESCROW"] = seal(doc["PUBLIC"], key, b"escrow")
    if p.exists():
        os.replace(p, p.with_name(f"recovery.json.old-{time.strftime('%Y%m%d-%H%M%S')}"))
    _write(p, doc)
    return rk


def refresh_escrow(cfg) -> bool:
    """After a rekey: seal the new CREDENTIALS key to the same public key (no recovery key needed)."""
    from . import vault
    doc = load_doc(cfg)
    if not doc:
        return False
    doc["ESCROW"] = seal(doc["PUBLIC"], vault.load_key(cfg), b"escrow")
    doc["ESCROW_UPDATED"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(path_for(cfg), doc)
    return True


def check(cfg, recovery_key: str) -> X25519PrivateKey:
    """The recovery key must open PRIVATE and the escrow, and the escrow must be the key in use."""
    import hmac
    from . import vault
    doc = load_doc(cfg)
    if not doc:
        raise RecoveryError("no recovery key yet: navigator-setup recovery init")
    priv = unlock(doc, recovery_key)
    if not hmac.compare_digest(unseal(priv, doc["ESCROW"], b"escrow"), vault.load_key(cfg)):
        raise RecoveryError("the recovery key is right, but its escrow is not the CREDENTIALS key in use "
                            "(run: navigator-setup recovery refresh)")
    return priv
