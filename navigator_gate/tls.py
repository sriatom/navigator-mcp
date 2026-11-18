"""A self-signed CA on the host and a server certificate signed by it (ECDSA P-256).

Files in tls_dir(): ca.key (600, never leaves the host), ca.pem (give this to clients), server.key (600),
server.pem. The CA lasts 10 years, the server certificate 825 days; `renew` makes a new server certificate
with the same CA, so clients keep trusting it.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .paths import tls_dir, write_private

CA_DAYS = 3650
SERVER_DAYS = 825


class TlsError(Exception):
    pass


def files(d: Path | None = None) -> dict:
    d = d or tls_dir()
    return {"dir": d, "ca_key": d / "ca.key", "ca": d / "ca.pem", "key": d / "server.key", "cert": d / "server.pem"}


def default_names() -> list[str]:
    """This host's names and addresses: hostname, FQDN, localhost, loopback and the primary IPv4."""
    names = [socket.gethostname(), socket.getfqdn(), "localhost", "127.0.0.1", "::1"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                    # no packet is sent; picks the outgoing address
            names.append(s.getsockname()[0])
    except OSError:
        pass
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    items = []
    for n in names:
        try:
            items.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            items.append(x509.DNSName(n))
    return x509.SubjectAlternativeName(items)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _pem_key(k) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())


def _make_ca(f: dict) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"NAVIGATOR local CA ({socket.gethostname()})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NAVIGATOR")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    write_private(f["ca_key"], _pem_key(key))
    write_private(f["ca"], cert.public_bytes(serialization.Encoding.PEM))
    f["ca"].chmod(0o644)


def _make_server(f: dict, names: list[str]) -> None:
    ca_key = serialization.load_pem_private_key(f["ca_key"].read_bytes(), None)
    ca = x509.load_pem_x509_certificate(f["ca"].read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=SERVER_DAYS))
            .add_extension(_san(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_private(f["key"], _pem_key(key))
    write_private(f["cert"], cert.public_bytes(serialization.Encoding.PEM))
    f["cert"].chmod(0o644)


def init(extra_names: list[str] | None = None, renew: bool = False, d: Path | None = None) -> dict:
    """Make the CA if there is none, and the server certificate if there is none (or renew=True)."""
    f = files(d)
    made = []
    if not f["ca"].is_file() or not f["ca_key"].is_file():
        if f["cert"].is_file():
            raise TlsError(f"{f['dir']}: a server certificate but no CA; move the old files aside first")
        _make_ca(f)
        made.append("CA")
    names = default_names() + [n for n in (extra_names or []) if n]
    names = list(dict.fromkeys(names))
    if renew or not f["cert"].is_file() or not f["key"].is_file():
        _make_server(f, names)
        made.append("server certificate")
    return {**info(d), "made": made}


def info(d: Path | None = None) -> dict:
    f = files(d)
    if not f["ca"].is_file():
        return {"ready": False, "dir": str(f["dir"])}
    ca = x509.load_pem_x509_certificate(f["ca"].read_bytes())
    out = {"ready": f["cert"].is_file() and f["key"].is_file(), "dir": str(f["dir"]), "ca_file": str(f["ca"]),
           "ca_subject": ca.subject.rfc4514_string(),
           "ca_sha256": ca.fingerprint(hashes.SHA256()).hex(":").upper(),
           "ca_expires": ca.not_valid_after_utc.date().isoformat()}
    if f["cert"].is_file():
        c = x509.load_pem_x509_certificate(f["cert"].read_bytes())
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        out.update(cert_file=str(f["cert"]), cert_expires=c.not_valid_after_utc.date().isoformat(),
                   names=[str(v) for v in san.get_values_for_type(x509.DNSName)]
                   + [str(v) for v in san.get_values_for_type(x509.IPAddress)],
                   days_left=(c.not_valid_after_utc - _now()).days)
    return out
