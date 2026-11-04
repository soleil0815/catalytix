"""TLS for the directory connection: fetch certificates directly from the domain controller.

Flow ("trust on first use", confirmed by the admin):
  1. Connect via LDAPS (636) or StartTLS (389) – still without verification.
  2. Read the server's certificate chain.
  3. If the root CA is missing, it is added:
       a) via the AIA address in the certificate (AD Certificate Services publish the CA there),
       b) from Active Directory itself (CN=Public Key Services in the configuration partition),
          provided a service account is configured.
  4. The admin sees issuer, validity and SHA-256 fingerprint and confirms.
  5. Preferably the root CA (+ intermediate CAs) is stored. Then the connection stays
     valid even after the DC certificate is renewed. Without a root CA the
     server certificate itself is used as the trust anchor.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import timezone
from urllib.parse import urlparse

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

from .i18n import _, format_day

log = logging.getLogger(__name__)
STARTTLS_OID = b"1.3.6.1.4.1.1466.20037"


class CertFetchError(Exception):
    pass


# ---------------------------------------------------------------- Helpers

def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def parse_server(entry: str, security: str) -> tuple[str, int, bool]:
    """'ldaps://dc1.firma.local:636' -> (host, port, ldaps?)"""
    entry = entry.strip()
    if "://" not in entry:
        entry = ("ldaps://" if security == "ldaps" else "ldap://") + entry
    u = urlparse(entry)
    ldaps = u.scheme.lower() == "ldaps" or security == "ldaps"
    port = u.port or (636 if ldaps else 389)
    return u.hostname or "", port, ldaps


def load_pem_bundle(text: str) -> list[x509.Certificate]:
    text = (text or "").strip()
    if not text:
        return []
    return x509.load_pem_x509_certificates(text.encode())


def to_pem(certs: list[x509.Certificate]) -> str:
    return "".join(c.public_bytes(Encoding.PEM).decode() for c in certs)


def is_self_signed(cert: x509.Certificate) -> bool:
    if cert.issuer != cert.subject:
        return False
    try:
        cert.verify_directly_issued_by(cert)
        return True
    except Exception:  # noqa: BLE001
        return False


def issued_by(cert: x509.Certificate, ca: x509.Certificate) -> bool:
    if cert.issuer != ca.subject:
        return False
    try:
        cert.verify_directly_issued_by(ca)
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca)
    except x509.ExtensionNotFound:
        return is_self_signed(cert)


def _names(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names = san.get_values_for_type(x509.DNSName) + [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    return names


@dataclass
class CertInfo:
    subject: str
    issuer: str
    not_after: str
    sha256: str
    names: list[str]
    kind: str  # "Stamm-CA" | "Zwischen-CA" | "Server" (root CA | intermediate CA | server)
    expired: bool


def describe(cert: x509.Certificate) -> CertInfo:
    from datetime import datetime
    na = cert.not_valid_after_utc
    if is_self_signed(cert):
        kind = "Stamm-CA" if _is_ca(cert) else "Selbstsigniert"
    elif _is_ca(cert):
        kind = "Zwischen-CA"
    else:
        kind = "Server"
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    return CertInfo(
        subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
        not_after=format_day(na.astimezone(timezone.utc).date()),
        sha256=":".join(fp[i:i + 2] for i in range(0, len(fp), 2)),
        names=_names(cert), kind=kind, expired=na < datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------- Fetching

def _read_ldap_message(sock: socket.socket) -> bytes:
    head = sock.recv(2)
    if len(head) < 2 or head[0] != 0x30:
        raise CertFetchError(_("Unerwartete Antwort – ist das ein LDAP-Server?"))
    length = head[1]
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(sock.recv(n), "big")
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _starttls(sock: socket.socket):
    oid = STARTTLS_OID
    ext = b"\x80" + bytes([len(oid)]) + oid
    op = b"\x77" + bytes([len(ext)]) + ext
    body = b"\x02\x01\x01" + op
    sock.sendall(b"\x30" + bytes([len(body)]) + body)
    resp = _read_ldap_message(sock)
    pos = resp.find(b"\x78")
    code_pos = resp.find(b"\x0a\x01", pos)
    if pos < 0 or code_pos < 0:
        raise CertFetchError(_("StartTLS-Antwort nicht verstanden."))
    if resp[code_pos + 2] != 0:
        raise CertFetchError(_("Server lehnt StartTLS ab (LDAP-Code {code}).", code=resp[code_pos + 2]))


def server_chain(host: str, port: int, ldaps: bool, timeout: int = 8) -> list[x509.Certificate]:
    """Read the certificate chain sent by the server (without verifying it)."""
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise CertFetchError(_("{host}:{port} nicht erreichbar ({error}).", host=host, port=port, error=exc)) from exc
    try:
        if not ldaps:
            _starttls(raw)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(raw, server_hostname=None if is_ip(host) else host) as tls:
            if hasattr(tls, "get_unverified_chain"):  # Python 3.13+: complete chain
                ders = [bytes(c) for c in tls.get_unverified_chain()]
            else:
                ders = [tls.getpeercert(binary_form=True)]
    except ssl.SSLError as exc:
        raise CertFetchError(_("TLS-Handshake mit {host}:{port} fehlgeschlagen ({error}).", host=host, port=port, error=exc.reason or exc)) from exc
    except OSError as exc:
        raise CertFetchError(_("Verbindung zu {host}:{port} abgebrochen ({error}).", host=host, port=port, error=exc)) from exc
    finally:
        try:
            raw.close()
        except OSError:
            pass
    certs = [x509.load_der_x509_certificate(d) for d in ders if d]
    if not certs:
        raise CertFetchError(_("Der Server hat kein Zertifikat gesendet."))
    return certs


def _aia_issuers(cert: x509.Certificate, timeout: int) -> list[x509.Certificate]:
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    found = []
    for desc in aia:
        if desc.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
            continue
        url = desc.access_location.value
        if not url.lower().startswith(("http://", "https://")):
            continue
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                continue
            data = r.content
            if b"-----BEGIN CERTIFICATE-----" in data:
                found += x509.load_pem_x509_certificates(data)
            else:
                try:
                    found.append(x509.load_der_x509_certificate(data))
                except ValueError:
                    # PKCS#7 bundle (.p7c)
                    from cryptography.hazmat.primitives.serialization import pkcs7
                    found += pkcs7.load_der_pkcs7_certificates(data)
        except Exception as exc:  # noqa: BLE001
            log.info("AIA fetch %s failed: %s", url, exc)
    return found


def _ad_ca_certs(directory) -> list[x509.Certificate]:
    """Read CA certificates from the AD configuration partition (needs a service account)."""
    from ldap3 import BASE, SUBTREE
    conn = directory.service_connection()
    try:
        conn.search("", "(objectClass=*)", BASE, attributes=["configurationNamingContext"])
        entries = [e for e in conn.response if e.get("type") == "searchResEntry"]
        cfg = entries[0]["attributes"].get("configurationNamingContext") if entries else None
        if isinstance(cfg, list):
            cfg = cfg[0] if cfg else None
        if not cfg:
            return []
        conn.search(f"CN=Public Key Services,CN=Services,{cfg}", "(cACertificate=*)", SUBTREE,
                    attributes=["cACertificate"])
        out = []
        for e in conn.response:
            if e.get("type") != "searchResEntry":
                continue
            for der in e.get("raw_attributes", {}).get("cACertificate", []):
                try:
                    out.append(x509.load_der_x509_certificate(der))
                except ValueError:
                    pass
        return out
    finally:
        conn.unbind()


def complete_chain(certs: list[x509.Certificate], pool: list[x509.Certificate]) -> list[x509.Certificate]:
    """Extend the chain with matching issuers from the pool until a root CA is reached."""
    chain = list(certs)
    for _ in range(6):
        last = chain[-1]
        if is_self_signed(last):
            break
        issuer = next((c for c in pool if issued_by(last, c) and c not in chain), None)
        if issuer is None:
            break
        chain.append(issuer)
    return chain


@dataclass
class FetchResult:
    trust: list[x509.Certificate]      # what is to be stored
    chain: list[x509.Certificate]      # complete chain determined (for display)
    notes: list[str]
    hostname_ok: dict[str, bool]


def fetch_trust_anchors(directory, servers: list[str], security: str, timeout: int = 8) -> FetchResult:
    """Fetch the certificates of all configured DCs and determine the trust anchors."""
    if security not in ("ldaps", "starttls") and not any(s.lower().startswith("ldaps://") for s in servers):
        raise CertFetchError(_("Bitte zuerst „LDAPS“ oder „StartTLS“ als Sicherheit wählen."))
    notes, chains, host_ok = [], [], {}
    for entry in servers:
        host, port, ldaps = parse_server(entry, security)
        if not host:
            continue
        chain = server_chain(host, port, ldaps, timeout)
        chains.append(chain)
        leaf = chain[0]
        host_ok[host] = hostname_matches(leaf, host)
    if not chains:
        raise CertFetchError(_("Kein Server konfiguriert."))

    pool = [c for ch in chains for c in ch]
    full = [complete_chain(ch, pool) for ch in chains]

    if not all(is_self_signed(ch[-1]) for ch in full):
        aia = []
        for ch in full:
            aia += _aia_issuers(ch[-1], timeout)
        if aia:
            pool += aia
            full = [complete_chain(ch, pool) for ch in full]
            if all(is_self_signed(ch[-1]) for ch in full):
                notes.append(_("Stamm-CA über die AIA-Adresse im Zertifikat ergänzt."))

    if not all(is_self_signed(ch[-1]) for ch in full) and directory is not None and directory.bind_dn:
        try:
            ad = _ad_ca_certs(directory)
            if ad:
                pool += ad
                full = [complete_chain(ch, pool) for ch in full]
                if all(is_self_signed(ch[-1]) for ch in full):
                    notes.append(_("Stamm-CA aus dem Active Directory (Public Key Services) ergänzt."))
        except Exception as exc:  # noqa: BLE001
            log.info("CA lookup in AD failed: %s", exc)

    trust, seen = [], set()
    for ch in full:
        if is_self_signed(ch[-1]):
            anchors = ch[1:] if len(ch) > 1 else ch  # CAs without the server certificate
        else:
            anchors = ch
            notes.append(_("Für „{subject}“ wurde keine Stamm-CA gefunden – "
                           "das Server-Zertifikat selbst wird vertraut. Nach einer Erneuerung bitte erneut abrufen.",
                           subject=ch[0].subject.rfc4514_string()))
        for c in anchors:
            fp = c.fingerprint(hashes.SHA256())
            if fp not in seen:
                seen.add(fp)
                trust.append(c)
    display, seen = [], set()
    for ch in full:
        for c in ch:
            fp = c.fingerprint(hashes.SHA256())
            if fp not in seen:
                seen.add(fp)
                display.append(c)
    return FetchResult(trust=trust, chain=display, notes=notes, hostname_ok=host_ok)


# ---------------------------------------------------------------- Verification when connecting

def make_tls(cadata: str, ca_file: str | None, verify: bool, check_host: bool):
    """ldap3 Tls object that uses the stored certificates as trust anchors."""
    from ldap3 import Tls

    class TrustTls(Tls):
        def wrap_socket(self, connection, do_handshake=False):
            host = connection.server.host
            if verify:
                ctx = ssl.create_default_context(cafile=ca_file or None, cadata=cadata or None)
                # Also accept a directly stored server/intermediate certificate as an anchor
                ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
                if hasattr(ssl, "VERIFY_X509_STRICT"):
                    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # older AD certificates are not always RFC-strict
            else:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.verify_mode = ssl.CERT_NONE
            ctx.check_hostname = False
            wrapped = ctx.wrap_socket(connection.socket, server_side=False, do_handshake_on_connect=True,
                                      server_hostname=None if is_ip(host) else host)
            if verify and check_host:
                _check_hostname(wrapped, host)
            connection.socket = wrapped

    return TrustTls(validate=ssl.CERT_REQUIRED if verify else ssl.CERT_NONE)


def hostname_matches(cert: x509.Certificate, host: str) -> bool:
    host = host.lower().strip("[]")
    if is_ip(host):
        try:
            san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
            return ipaddress.ip_address(host) in san.get_values_for_type(x509.IPAddress)
        except x509.ExtensionNotFound:
            return False
    for name in (n.lower() for n in _names(cert)):
        if name == host:
            return True
        if name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def _check_hostname(wrapped, host: str):
    from ldap3.core.exceptions import LDAPCertificateError
    der = wrapped.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    if not hostname_matches(cert, host):
        names = ", ".join(_names(cert)) or cert.subject.rfc4514_string()
        raise LDAPCertificateError(
            _("Hostname '{host}' steht nicht im Zertifikat des Servers (enthalten: {names}). "
              "Bitte den DNS-Namen des DCs als Server eintragen oder die Hostname-Prüfung abschalten.",
              host=host, names=names))
