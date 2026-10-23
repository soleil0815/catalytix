"""Tests für das Abrufen und Prüfen der DC-Zertifikate."""
import datetime as dt
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-tls-"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from catalytix import ldap_tls as T  # noqa: E402


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_pki(tmp):
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Test Root CA")).issuer_name(_name("Test Root CA"))
          .public_key(ca_key.public_key()).serial_number(1).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(_name("dc1.test.local")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("dc1.test.local"), x509.DNSName("localhost")]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    cert_path, key_path = os.path.join(tmp, "dc.pem"), os.path.join(tmp, "dc.key")
    with open(cert_path, "wb") as f:
        f.write(leaf.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return ca, leaf, cert_path, key_path


class TLSServer:
    """Minimaler LDAPS-Ersatz: TLS-Handshake, danach Verbindung schließen."""

    def __init__(self, cert, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                t = self.ctx.wrap_socket(c, server_side=True)
                t.settimeout(1)
                try:
                    t.recv(16)
                except OSError:
                    pass
                t.close()
            except (ssl.SSLError, OSError):
                c.close()

    def close(self):
        self.sock.close()


class CertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ca, cls.leaf, cert, key = make_pki(cls.tmp)
        cls.server = TLSServer(cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_parse_server(self):
        self.assertEqual(T.parse_server("dc1.x.local", "ldaps"), ("dc1.x.local", 636, True))
        self.assertEqual(T.parse_server("ldap://dc1:3268", "starttls"), ("dc1", 3268, False))
        self.assertEqual(T.parse_server("ldaps://dc2", "none"), ("dc2", 636, True))

    def test_requires_tls_mode(self):
        with self.assertRaises(T.CertFetchError):
            T.fetch_trust_anchors(None, ["dc1"], "none")

    def test_unreachable(self):
        with self.assertRaises(T.CertFetchError):
            T.server_chain("127.0.0.1", 1, True, 2)

    def test_fetch_leaf_without_root(self):
        with mock.patch.object(T, "_aia_issuers", return_value=[]):
            res = T.fetch_trust_anchors(None, [f"ldaps://localhost:{self.server.port}"], "ldaps", 5)
        self.assertEqual(res.trust, [self.leaf])
        self.assertTrue(res.notes)
        self.assertTrue(res.hostname_ok["localhost"])

    def test_fetch_root_via_aia(self):
        with mock.patch.object(T, "_aia_issuers", return_value=[self.ca]):
            res = T.fetch_trust_anchors(None, [f"localhost:{self.server.port}"], "ldaps", 5)
        self.assertEqual(res.trust, [self.ca])
        self.assertEqual(T.describe(res.trust[0]).kind, "Stamm-CA")

    def test_fetch_root_from_ad(self):
        directory = mock.Mock(bind_dn="CN=svc")
        with mock.patch.object(T, "_aia_issuers", return_value=[]), \
             mock.patch.object(T, "_ad_ca_certs", return_value=[self.ca]):
            res = T.fetch_trust_anchors(directory, [f"localhost:{self.server.port}"], "ldaps", 5)
        self.assertEqual(res.trust, [self.ca])

    def test_verification_with_anchors(self):
        from ldap3 import Connection, Server
        from ldap3.core.exceptions import LDAPException

        def open_with(pem, host="localhost", check=True):
            s = Server(host, port=self.server.port, use_ssl=True, tls=T.make_tls(pem, None, True, check),
                       connect_timeout=5)
            Connection(s).open(read_server_info=False)

        open_with(T.to_pem([self.ca]))           # Stamm-CA
        open_with(T.to_pem([self.leaf]))         # Server-Zertifikat direkt
        with self.assertRaises(LDAPException):
            open_with("")                        # unbekannte CA
        with self.assertRaises(LDAPException):
            open_with(T.to_pem([self.ca]), host="127.0.0.1")  # IP nicht im Zertifikat
        open_with(T.to_pem([self.ca]), host="127.0.0.1", check=False)

    def test_pem_roundtrip_and_describe(self):
        pem = T.to_pem([self.ca, self.leaf])
        certs = T.load_pem_bundle(pem)
        self.assertEqual(certs, [self.ca, self.leaf])
        info = T.describe(self.leaf)
        self.assertEqual(info.kind, "Server")
        self.assertIn("localhost", info.names)
        self.assertEqual(len(info.sha256.split(":")), 32)


class AdminCertFlowTests(unittest.TestCase):
    def setUp(self):
        from catalytix import create_app
        from catalytix.models import User, db
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        u = User(username="admin", role="admin")
        u.set_password("geheim123")
        db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()
        self.c.post("/login", data={"username": "admin", "password": "geheim123"})
        _, self.leaf, _, _ = make_pki(tempfile.mkdtemp())
        self.ca = T.load_pem_bundle(T.to_pem([self.leaf]))[0]

    def tearDown(self):
        from catalytix.models import db
        db.session.remove()
        self.ctx.pop()

    def test_fetch_trust_clear(self):
        from catalytix.models import get_setting
        res = T.FetchResult(trust=[self.leaf], chain=[self.leaf], notes=["Hinweis"], hostname_ok={"dc1": False})
        form = {"ldap_server": "ldaps://dc1", "ldap_security": "ldaps", "ldap_base_dn": "DC=x"}
        with mock.patch.object(T, "fetch_trust_anchors", return_value=res):
            html = self.c.post("/admin/verzeichnisdienst", data={**form, "action": "fetch_cert"}).get_data(as_text=True)
        self.assertIn("Vertrauen", html)
        self.assertIn("steht nicht im Server-Zertifikat", html)
        pem = T.to_pem([self.leaf])
        self.c.post("/admin/verzeichnisdienst", data={**form, "action": "trust_cert", "pending_pem": pem})
        self.assertEqual(get_setting("ldap_tls_certs"), pem)
        self.assertEqual(get_setting("ldap_server"), "ldaps://dc1")
        self.assertIn("1 hinterlegt", self.c.get("/admin/verzeichnisdienst").get_data(as_text=True))
        self.c.post("/admin/verzeichnisdienst", data={**form, "action": "clear_cert"})
        self.assertEqual(get_setting("ldap_tls_certs"), "")

    def test_fetch_error_shown(self):
        html = self.c.post("/admin/verzeichnisdienst", data={"ldap_server": "dc1", "ldap_security": "none",
                                                             "action": "fetch_cert"}).get_data(as_text=True)
        self.assertIn("LDAPS", html)

    def test_invalid_pem_rejected(self):
        r = self.c.post("/admin/verzeichnisdienst", data={"action": "trust_cert", "pending_pem": "kaputt"},
                        follow_redirects=True)
        self.assertIn("Keine gültigen Zertifikate", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
