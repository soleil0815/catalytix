"""Tests für den E-Mail-Versand – mit einem echten (Mini-)SMTP-Server im Test.

Der Testserver spricht SMTP inkl. STARTTLS, implizitem TLS und AUTH PLAIN/LOGIN,
damit der Versandweg wirklich über smtplib und das Netzwerk läuft."""
import base64
import datetime as dt
import email
import os
import socket
import socketserver
import ssl
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from email import policy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-mail-"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from catalytix import create_app, mailer  # noqa: E402
from catalytix.models import Book, Loan, MailLog, User, db, set_setting  # noqa: E402


def self_signed(tmp):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    c, k = os.path.join(tmp, "smtp.pem"), os.path.join(tmp, "smtp.key")
    open(c, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(k, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return c, k


class FakeSMTP:
    """Minimaler SMTP-Server (RFC 5321) für Tests."""

    def __init__(self, mode="plain", user=None, password=None, cert=None):
        self.mode, self.user, self.password = mode, user, password
        self.messages = []
        self.ctx = None
        if cert:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*cert)
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    sock = self.request
                    if outer.mode == "ssl":
                        sock = outer.ctx.wrap_socket(sock, server_side=True)
                    self.run(sock)
                except (ssl.SSLError, OSError):
                    pass  # z. B. Client lehnt Testzertifikat ab – gewollt im Test

            def run(self, sock):
                f = sock.makefile("rb")
                tls = outer.mode == "ssl"
                authed = outer.user is None
                mail_from, rcpts = None, []

                def send(line):
                    sock.sendall((line + "\r\n").encode())

                send("220 fake ESMTP")
                while True:
                    line = f.readline()
                    if not line:
                        return
                    cmd = line.decode().strip()
                    up = cmd.upper()
                    if up.startswith(("EHLO", "HELO")):
                        ext = ["250-fake"]
                        if outer.mode == "starttls" and not tls:
                            ext.append("250-STARTTLS")
                        if outer.user and (tls or outer.mode == "plain"):
                            ext.append("250-AUTH PLAIN LOGIN")
                        ext.append("250 8BITMIME")
                        for e in ext:
                            send(e)
                    elif up == "STARTTLS":
                        send("220 go ahead")
                        f.close()
                        sock = outer.ctx.wrap_socket(sock, server_side=True)
                        f = sock.makefile("rb")
                        tls = True
                    elif up.startswith("AUTH PLAIN"):
                        parts = cmd.split()
                        data = parts[2] if len(parts) > 2 else None
                        if data is None:
                            send("334 ")
                            data = f.readline().decode().strip()
                        _, u, p = base64.b64decode(data).decode().split("\0")
                        authed = (u, p) == (outer.user, outer.password)
                        send("235 ok" if authed else "535 5.7.8 Authentication failed")
                    elif up.startswith("AUTH LOGIN"):
                        send("334 VXNlcm5hbWU6")
                        u = base64.b64decode(f.readline().strip()).decode()
                        send("334 UGFzc3dvcmQ6")
                        p = base64.b64decode(f.readline().strip()).decode()
                        authed = (u, p) == (outer.user, outer.password)
                        send("235 ok" if authed else "535 5.7.8 Authentication failed")
                    elif up.startswith("MAIL FROM"):
                        if not authed:
                            send("530 5.7.0 Authentication required")
                            continue
                        mail_from, rcpts = cmd[10:].strip(), []
                        send("250 ok")
                    elif up.startswith("RCPT TO"):
                        addr = cmd[8:].strip().strip("<>")
                        if addr.startswith("reject"):
                            send("550 no such user")
                        else:
                            rcpts.append(addr)
                            send("250 ok")
                    elif up == "DATA":
                        send("354 end with .")
                        data = b""
                        while True:
                            l = f.readline()
                            if l in (b".\r\n", b".\n", b""):
                                break
                            data += l[1:] if l.startswith(b"..") else l
                        outer.messages.append({"from": mail_from, "to": rcpts,
                                               "msg": email.message_from_bytes(data, policy=policy.default)})
                        send("250 queued")
                    elif up == "QUIT":
                        send("221 bye")
                        return
                    elif up in ("RSET", "NOOP"):
                        send("250 ok")
                    else:
                        send("502 not implemented")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def body_text(msg):
    return msg.get_body(("plain",)).get_content()


def body_html(msg):
    return msg.get_body(("html",)).get_content()


class MailBase(unittest.TestCase):
    smtp_mode = "plain"
    smtp_user = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cert = self_signed(self.tmp)
        self.smtp = FakeSMTP(self.smtp_mode, user=self.smtp_user, password="pw", cert=cert)
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        for k, v in {"mail_enabled": "1", "smtp_host": "localhost", "smtp_port": str(self.smtp.port),
                     "smtp_security": {"plain": "none"}.get(self.smtp_mode, self.smtp_mode), "smtp_from": "bibliothek@schule.de",
                     "smtp_from_name": "Schulbibliothek", "smtp_verify_cert": "0",
                     "smtp_user": self.smtp_user or "", "smtp_password": "pw",
                     "app_base_url": "https://bib.schule.local", "library_name": "Schulbibliothek"}.items():
            set_setting(k, v)
        db.session.commit()
        self.admin = self._user("admin", "admin", "admin@schule.de")
        self.lena = self._user("lena", "reader", "lena@schule.de", display="Lena Schmidt")
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        self.smtp.close()

    def _user(self, name, role, mail, display=None):
        u = User(username=name, display_name=display or name, role=role, email=mail)
        u.set_password("geheim123")
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": "geheim123"})

    def book(self, title="Momo", copies=1):
        b = Book(title=title, authors="Michael Ende", copies=copies, location="Regal A1")
        db.session.add(b)
        db.session.commit()
        return b

    def loan(self, user, book, due):
        l = Loan(book=book, user=user, due_at=due, loaned_at=datetime.now() - timedelta(days=20))
        db.session.add(l)
        db.session.commit()
        return l


class SendTests(MailBase):
    def test_loan_and_return_confirmation(self):
        set_setting("mail_on_return", "1")
        db.session.commit()
        b = self.book()
        self.login("lena")
        self.c.post(f"/buecher/{b.id}/ausleihen")
        self.assertEqual(len(self.smtp.messages), 1)
        m = self.smtp.messages[0]["msg"]
        self.assertEqual(self.smtp.messages[0]["to"], ["lena@schule.de"])
        self.assertEqual(m["Subject"], "Ausgeliehen: Momo")
        self.assertIn("Schulbibliothek", m["From"])
        self.assertIn("Rückgabe bis", body_text(m))
        self.assertIn("https://bib.schule.local/ausleihen", body_html(m))
        loan = Loan.query.one()
        self.login("admin")
        self.c.post(f"/ausleihen/{loan.id}/rueckgabe")
        self.assertEqual(self.smtp.messages[-1]["msg"]["Subject"], "Zurückgegeben: Momo")
        self.assertEqual(MailLog.query.filter_by(status="sent").count(), 2)

    def test_no_mail_when_disabled_or_opted_out(self):
        b = self.book(copies=3)
        self.lena.email_notifications = False
        db.session.commit()
        self.login("lena")
        self.c.post(f"/buecher/{b.id}/ausleihen")
        self.assertEqual(self.smtp.messages, [])
        set_setting("mail_enabled", "0")
        self.lena.email_notifications = True
        db.session.commit()
        self.login("admin")
        self.c.post(f"/buecher/{b.id}/ausleihen")
        self.assertEqual(self.smtp.messages, [])

    def test_reminders_grouped_and_not_repeated(self):
        today = date.today()
        b1, b2, b3 = self.book("Momo"), self.book("Krabat"), self.book("Tschick")
        self.loan(self.lena, b1, today + timedelta(days=1))   # bald fällig
        self.loan(self.lena, b2, today - timedelta(days=3))   # überfällig
        self.loan(self.lena, b3, today + timedelta(days=20))  # noch lange Zeit
        stats = mailer.run_notifications(today)
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["loans"], 2)
        self.assertEqual(len(self.smtp.messages), 1)  # eine Mail für beide Bücher
        text = body_text(self.smtp.messages[0]["msg"])
        self.assertIn("Krabat", text)
        self.assertIn("3 Tage überfällig", text)
        self.assertIn("Momo", text)
        self.assertNotIn("Tschick", text)
        # gleicher Tag: nichts Neues
        mailer.run_notifications(today)
        self.assertEqual(len(self.smtp.messages), 1)

    def test_overdue_repeat_and_maximum(self):
        set_setting("mail_overdue_repeat_days", "7")
        set_setting("mail_overdue_max", "2")
        db.session.commit()
        today = date.today()
        l = self.loan(self.lena, self.book(), today - timedelta(days=1))
        mailer.run_notifications(today)
        self.assertEqual(len(self.smtp.messages), 1)
        mailer.run_notifications(today + timedelta(days=3))   # zu früh für Wiederholung
        self.assertEqual(len(self.smtp.messages), 1)
        # Wiederholung nach 7 Tagen (Protokolldatum zurückdatieren)
        MailLog.query.filter_by(loan_id=l.id).update({"created_at": datetime.now() - timedelta(days=8)})
        db.session.commit()
        mailer.run_notifications(today)
        self.assertEqual(len(self.smtp.messages), 2)
        MailLog.query.filter_by(loan_id=l.id).update({"created_at": datetime.now() - timedelta(days=8)})
        db.session.commit()
        mailer.run_notifications(today)   # Maximum 2 erreicht
        self.assertEqual(len(self.smtp.messages), 2)

    def test_digest_to_librarian(self):
        set_setting("mail_digest", "1")
        db.session.commit()
        self.loan(self.lena, self.book(), date.today() - timedelta(days=5))
        mailer.run_notifications()
        subjects = [m["msg"]["Subject"] for m in self.smtp.messages]
        self.assertIn("Überfällige Ausleihen: 1", subjects)
        digest = next(m for m in self.smtp.messages if m["msg"]["Subject"].startswith("Überfällige"))
        self.assertEqual(digest["to"], ["admin@schule.de"])
        self.assertIn("Lena Schmidt", body_html(digest["msg"]))

    def test_scheduler_runs_once_per_day(self):
        set_setting("mail_hour", "7")
        db.session.commit()
        self.loan(self.lena, self.book(), date.today() - timedelta(days=1))
        early = datetime.combine(date.today(), dt.time(6, 0))
        self.assertIsNone(mailer.run_scheduled(early))
        later = datetime.combine(date.today(), dt.time(7, 5))
        self.assertIsNotNone(mailer.run_scheduled(later))
        self.assertIsNone(mailer.run_scheduled(later))   # zweiter Prozess / zweiter Aufruf: nichts
        self.assertEqual(len(self.smtp.messages), 1)

    def test_manual_reminder_button(self):
        l = self.loan(self.lena, self.book(), date.today() - timedelta(days=2))
        self.login("admin")
        r = self.c.post(f"/ausleihen/{l.id}/erinnern", follow_redirects=True)
        self.assertIn("Erinnerung an lena@schule.de gesendet", r.get_data(as_text=True))
        self.assertEqual(len(self.smtp.messages), 1)

    def test_rejected_recipient_logged(self):
        self.lena.email = "reject@schule.de"
        db.session.commit()
        l = self.loan(self.lena, self.book(), date.today())
        ok, err = mailer.send_manual_reminder(l)
        self.assertFalse(ok)
        self.assertTrue(MailLog.query.filter_by(status="error").count())

    def test_admin_page_test_mail_and_save(self):
        self.login("admin")
        r = self.c.post("/admin/email", data={
            "action": "test", "test_to": "it@schule.de", "smtp_host": "localhost",
            "smtp_port": str(self.smtp.port), "smtp_security": "none",
            "smtp_from": "bibliothek@schule.de", "mail_enabled": "1"}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Testmail an it@schule.de gesendet", html)
        self.assertEqual(self.smtp.messages[-1]["msg"]["Subject"], "Testnachricht von Schulbibliothek")
        from catalytix.models import get_setting
        same = {"smtp_host": get_setting("smtp_host"), "smtp_user": get_setting("smtp_user")}
        self.c.post("/admin/email", data={"action": "save", **same, "smtp_port": "465",
                                          "smtp_security": "ssl", "smtp_from": "a@b.de", "mail_enabled": "1"})
        self.assertEqual(get_setting("smtp_security"), "ssl")
        self.assertEqual(get_setting("smtp_password"), "pw")   # leeres Feld behält Passwort (gleicher Server)
        self.c.post("/admin/email", data={"action": "save", "smtp_host": "mail.anders.example", "smtp_port": "465",
                                          "smtp_security": "ssl", "smtp_from": "a@b.de", "mail_enabled": "1"})
        self.assertEqual(get_setting("smtp_password"), "")     # anderer Server bekommt das alte Passwort nicht
        self.assertEqual(self.c.get("/admin/email").status_code, 200)

    def test_test_mail_error_is_shown(self):
        self.login("admin")
        r = self.c.post("/admin/email", data={"action": "test", "test_to": "x@y.de", "smtp_host": "127.0.0.1",
                                              "smtp_port": "1", "smtp_security": "none", "smtp_from": "a@b.de"},
                        follow_redirects=True)
        self.assertIn("Testmail fehlgeschlagen", r.get_data(as_text=True))

    def test_profile_opt_out(self):
        self.login("lena")
        self.c.post("/profil", data={"action": "notify", "email": "neu@schule.de"})
        db.session.refresh(self.lena)
        self.assertFalse(self.lena.email_notifications)
        self.assertEqual(self.lena.email, "neu@schule.de")


class StartTlsAuthTests(MailBase):
    smtp_mode = "starttls"
    smtp_user = "bib"

    def test_starttls_with_login(self):
        l = self.loan(self.lena, self.book(), date.today())
        ok, err = mailer.send_manual_reminder(l)
        self.assertTrue(ok, err)
        self.assertEqual(len(self.smtp.messages), 1)

    def test_wrong_password(self):
        set_setting("smtp_password", "falsch")
        db.session.commit()
        l = self.loan(self.lena, self.book(), date.today())
        ok, err = mailer.send_manual_reminder(l)
        self.assertFalse(ok)
        self.assertIn("Anmeldung am SMTP-Server fehlgeschlagen", err)

    def test_certificate_verification(self):
        set_setting("smtp_verify_cert", "1")
        db.session.commit()
        l = self.loan(self.lena, self.book(), date.today())
        ok, err = mailer.send_manual_reminder(l)
        self.assertFalse(ok)
        self.assertIn("Zertifikat", err)


class ImplicitTlsTests(MailBase):
    smtp_mode = "ssl"
    smtp_user = "bib"

    def test_ssl_with_login(self):
        l = self.loan(self.lena, self.book(), date.today())
        ok, err = mailer.send_manual_reminder(l)
        self.assertTrue(ok, err)


class MigrationTests(unittest.TestCase):
    def test_missing_column_is_added(self):
        import sqlalchemy as sa
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        eng = sa.create_engine(f"sqlite:///{path}")
        with eng.begin() as c:   # Tabelle wie in Version 1.2 – ohne email_notifications
            c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(120) NOT NULL UNIQUE, "
                              "display_name VARCHAR(200), email VARCHAR(200), password_hash VARCHAR(256), "
                              "auth_source VARCHAR(10), ldap_dn VARCHAR(500), role VARCHAR(20) NOT NULL, "
                              "active BOOLEAN NOT NULL, created_at DATETIME, last_login DATETIME)"))
            c.execute(sa.text("INSERT INTO users (username, role, active) VALUES ('alt', 'admin', 1)"))
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}"})
        with app.app_context():
            u = User.query.filter_by(username="alt").one()
            self.assertTrue(u.email_notifications)
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
