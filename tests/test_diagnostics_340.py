"""3.4.0: anonymous diagnostic package. Two kinds of tests:

* no leaks – a database full of recognisable names, addresses and secrets must not show up in the package;
* keeps up with the program – every setting and every table must be classified in diagnostics.py, so nothing new
  slips into the package unnoticed (or is missing from it).
"""
import glob
import io
import logging
import os
import re
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import create_app, diagnostics, dbversion  # noqa: E402
from catalytix.models import (DEFAULT_SETTINGS, DiagAlias, DirectoryPerson, Group, Location, LoginAttempt, User,  # noqa: E402
                              db, get_setting, set_setting)

ROOT = Path(__file__).resolve().parent.parent
SETTING_CALL = re.compile(r"(?:\b(?:get_setting|set_setting|get_bool|get_int|get_int_setting|_write_setting)|(?<![\w.])s)\(\s*"
                          r"(?:db,\s*)?[\"']([a-z][a-z0-9_]+)[\"']")


def unpack(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {Path(n).name: z.read(n).decode("utf-8") for n in z.namelist()}


class ClassificationTests(unittest.TestCase):
    """New settings or tables must be sorted into the diagnostic package – otherwise this fails."""

    def test_groups_do_not_overlap(self):
        self.assertFalse(diagnostics.SECRET & diagnostics.NAMES)
        self.assertFalse(diagnostics.SECRET & diagnostics.PLAIN)
        self.assertFalse(diagnostics.NAMES & diagnostics.PLAIN)
        groups = [diagnostics.SECRET, diagnostics.NAMES, diagnostics.PLAIN, diagnostics.FREE_TEXT]
        for i, a in enumerate(groups):
            for b in groups[i + 1:]:
                self.assertFalse(a & b, a & b)

    def known_keys(self):
        keys = set(DEFAULT_SETTINGS)
        extra = re.compile(r"(?:\bkey\s*=\s*|Setting\.key\s*==\s*|\bkey\s*:\s*str\s*=\s*)[\"']([a-z][a-z0-9_]+)[\"']")
        for f in glob.glob(str(ROOT / "catalytix" / "**" / "*.py"), recursive=True):
            src = Path(f).read_text(encoding="utf-8")
            keys |= set(SETTING_CALL.findall(src)) | {k for k in extra.findall(src) if "_" in k}
        for f in glob.glob(str(ROOT / "catalytix" / "templates" / "**" / "*.html"), recursive=True):
            keys |= set(re.findall(r"\bs\(['\"]([a-z][a-z0-9_]+)['\"]\)", Path(f).read_text(encoding="utf-8")))
        from catalytix import KEY_CHECK_SETTING
        from catalytix.reading_sample import SETTING
        keys |= {dbversion.KEY, dbversion.BACKUP_KEY, KEY_CHECK_SETTING, SETTING}
        return keys

    def test_every_setting_is_classified(self):
        classified = diagnostics.SECRET | diagnostics.NAMES | diagnostics.PLAIN | diagnostics.FREE_TEXT
        missing = sorted(self.known_keys() - classified)
        self.assertEqual(missing, [], "Diese Einstellungen sind im Diagnosepaket nicht eingeordnet – in "
                                      "catalytix/diagnostics.py zu SECRET, NAMES oder PLAIN hinzufügen: "
                                      f"{missing}")

    def test_secret_settings_are_secret(self):
        from catalytix.models import SECRET_SETTINGS
        self.assertLessEqual(SECRET_SETTINGS, diagnostics.SECRET)

    def test_every_table_is_listed(self):
        tables = {t.name for t in db.metadata.sorted_tables}
        self.assertEqual(sorted(tables - set(diagnostics.TABLES)), [],
                         "Neue Tabelle – in diagnostics.TABLES eintragen (das Paket nimmt nur die Anzahl)")


class LeakTests(BaseCase):
    """Recognisable values everywhere – none of them may appear in the package."""

    FORBIDDEN = ["zorgmeister", "quengelmann", "zacharias", "geheimfirma", "quarkhausen", "zebra", "kellerarchiv",
                 "dc07", "10.20.30.40", "203.0.113.77", "2001:db8:abcd::77", "sup3rgeheim", "smtpgeheim",
                 "aizasyd-geheim", "ylva", "müllerbrot", "pc-buchhaltung", "quark.dyndns", "versand@",
                 "lehrkraefte", "totpgeheim", "fernetgeheim",
                 # 3.4.1: free texts, SMTP account and group names of the rules
                 "qxz", "fantasiegesellschaft", "zugspitzforschung", "mx7f3k9q", "bib-oberaufsicht", "leseratten-team"]

    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")
        u = User(username="zorgmeister", display_name="Zacharias Quengelmann", email="zq@geheimfirma.de",
                 role="reader", auth_source="ldap",
                 ldap_dn="CN=Zacharias Quengelmann,OU=Lehrkraefte,DC=geheimfirma,DC=intra")
        db.session.add(u)
        g = Group(name="Geheimgruppe Zebra", ldap_dn="CN=Geheimgruppe Zebra,OU=Lehrkraefte,DC=geheimfirma,DC=intra")
        db.session.add(g)
        db.session.add(Location(name="Kellerarchiv Nord"))
        db.session.add(DirectoryPerson(username="ylva.m", username_key="ylva.m", display_name="Ylva Müllerbrot",
                                       email="ylva@geheimfirma.de"))
        db.session.add(LoginAttempt(key="zorgmeister|203.0.113.77", username="zorgmeister", ip="203.0.113.77"))
        for k, v in {"library_name": "Stadtbibliothek Quarkhausen", "ldap_enabled": "0",
                     "ldap_server": "dc07.geheimfirma.intra, 10.20.30.40",
                     "ldap_bind_dn": "CN=svc-bib,OU=Lehrkraefte,DC=geheimfirma,DC=intra",
                     "ldap_base_dn": "DC=geheimfirma,DC=intra", "ldap_upn_suffix": "@geheimfirma.intra",
                     "ldap_login_rules": "CN=Geheimgruppe Zebra,OU=Lehrkraefte,DC=geheimfirma,DC=intra\nLESERATTEN-TEAM",
                     "ldap_admin_rules": "BIB-OBERAUFSICHT",
                     "smtp_from_name": "QXZ Bibliothek - Fantasiegesellschaft für Zugspitzforschung",
                     "ldap_bind_password": "Sup3rGeheim!Pw", "smtp_host": "mail.geheimfirma.de",
                     "smtp_user": "mx7f3k9q", "smtp_password": "SmtpGeheim#42",
                     "smtp_from": "versand@geheimfirma.de", "google_api_key": "AIzaSyD-geheimgeheimgeheimgeheim12",
                     "app_base_url": "https://bib.geheimfirma.de", "own_public_hosts": "quark.dyndns.org",
                     "mail_footer": "Stadtbibliothek Quarkhausen · Kellerarchiv Nord", "registration_domains":
                     "geheimfirma.de", "login_text": "Willkommen in Quarkhausen"}.items():
            set_setting(k, v)
        db.session.commit()
        self.logdir = Path(self.app.instance_path) / diagnostics.LOG_DIR
        self.logdir.mkdir(exist_ok=True)
        self.logfile = self.logdir / f"catalytix-{date.today():%Y-%m-%d}.log"
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S},123"
        self.logfile.write_text("\n".join([
            f"{stamp} INFO catalytix.views.main: Sign-in of zorgmeister from 203.0.113.77",
            f"{stamp} WARNING catalytix.lockout: Login failed for Zacharias Quengelmann (2001:db8:abcd::77)",
            f"{stamp} ERROR catalytix.ldap_auth: LDAP bind to dc07.geheimfirma.intra (10.20.30.40) failed: "
            "invalid credentials password=Sup3rGeheim!Pw",
            "Traceback (most recent call last):",
            '  File "/app/catalytix/ldap_auth.py", line 250, in service_connection',
            "    conn = self.service_connection()",
            "ldap3.core.exceptions.LDAPBindError: CN=svc-bib,OU=Lehrkraefte,DC=geheimfirma,DC=intra",
            f"{stamp} INFO catalytix.mailer: Sent mail to zq@geheimfirma.de via mail.geheimfirma.de",
            f"{stamp} INFO catalytix.people: unknown host pc-buchhaltung.geheimfirma.intra asked",
            f"{stamp} INFO catalytix.x: totp_secret=TOTPGEHEIMABCDEF token: fernetgeheimtoken123",
            f"{stamp} INFO catalytix.x: stored enc:gAAAAABfernetgeheimfernetgeheimfernetgeheim",
            f"{stamp} INFO catalytix.x: user ylva.m (Ylva Müllerbrot) synced",
            f"{stamp} INFO catalytix.mailer: Sent as \"QXZ Bibliothek - Fantasiegesellschaft für Zugspitzforschung\" "
            "via mx7f3k9q, member of BIB-OBERAUFSICHT and Leseratten-Team",
            f"{stamp} INFO catalytix.ldap_auth: Verbindung erfolgreich. Angemeldet als CN=svc-bib,OU=Lehrkraefte,"
            "DC=geheimfirma,DC=intra",
        ]) + "\n", encoding="utf-8")
        self.login("admin")

    def tearDown(self):
        self.logfile.unlink(missing_ok=True)
        super().tearDown()

    def package(self, **form):
        r = self.c.post("/admin/diagnosepaket", data={"tage": "7", **form})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        return unpack(r.data)

    def test_nothing_recognisable_in_the_package(self):
        files = self.package(ohne_tests="1")
        self.assertEqual(sorted(files), ["LIESMICH.txt", "checks.json", "counts.json", "errors.txt", "https.json",
                                         "log.txt", "settings.json", "system.json"])
        everything = "\n".join(files.values()).lower()
        leaks = [w for w in self.FORBIDDEN if w in everything]
        self.assertEqual(leaks, [], "\n".join(f"{n}: {line}" for n, c in files.items() for line in c.splitlines()
                                              if any(w in line.lower() for w in leaks))[:3000])

    def test_but_useful(self):
        files = self.package(ohne_tests="1")
        log = files["log.txt"]
        self.assertIn("BENUTZER-", log)
        self.assertIn("(öffentlich)", log)                    # kind of address stays visible
        self.assertIn("LDAPBindError", files["errors.txt"])   # technical details stay
        self.assertIn("Traceback", files["errors.txt"])
        self.assertNotIn("Sign-in of", files["errors.txt"])   # only warnings and errors
        self.assertIn('"ldap_bind_password *": "(gesetzt)"', files["settings.json"])
        self.assertIn('"passkey_login": "1"', files["settings.json"])
        self.assertIn('"smtp_from_name *": "(eigener Text, ', files["settings.json"])
        self.assertIn('"login_eyebrow": "(Vorgabe)"', files["settings.json"])
        self.assertNotIn("nicht eingeordnet", files["settings.json"])
        # the same AD path looks the same in the settings and in the log
        import json
        bind = json.loads(files["settings.json"])["ldap_bind_dn *"]
        self.assertIn(f"Angemeldet als {bind}", files["log.txt"])
        self.assertIn('"mit_passkey": 0', files["counts.json"])
        self.assertIn('"version": "', files["system.json"])

    def test_placeholders_stay_the_same(self):
        first = self.package(ohne_tests="1")["log.txt"]
        alias = DiagAlias.query.filter_by(kind="user", real="zorgmeister").one().alias
        self.assertIn(f"Sign-in of {alias} from", first)
        second = self.package(ohne_tests="1")["log.txt"]
        self.assertIn(f"Sign-in of {alias} from", second)
        self.assertEqual(DiagAlias.query.filter_by(kind="user", real="zorgmeister").count(), 1)

    def test_lookup_and_csv(self):
        self.package(ohne_tests="1")
        alias = DiagAlias.query.filter_by(kind="user", real="zorgmeister").one().alias
        page = self.c.get(f"/admin/einstellungen?bereich=wartung&platzhalter={alias}").get_data(as_text=True)
        self.assertIn("zorgmeister", page)
        csv = self.c.post("/admin/diagnosepaket/platzhalter.csv").get_data(as_text=True)
        self.assertIn(f"{alias};zorgmeister", csv)

    def test_only_admins(self):
        self.make_user("leser")
        self.c.post("/logout")
        self.login("leser")
        self.assertNotEqual(self.c.post("/admin/diagnosepaket", data={"tage": "7"}).status_code, 200)
        self.assertNotEqual(self.c.post("/admin/diagnosepaket/platzhalter.csv").status_code, 200)
        self.assertNotEqual(self.c.get("/admin/einstellungen?bereich=wartung").status_code, 200)

    def test_live_checks_are_anonymous_too(self):
        set_setting("ldap_enabled", "1")
        set_setting("mail_enabled", "1")
        set_setting("ldap_timeout", "1")
        db.session.commit()
        from unittest import mock

        from catalytix.ldap_auth import Directory, LDAPError
        with mock.patch.object(Directory, "test_connection",
                               side_effect=LDAPError("dc07.geheimfirma.intra nicht erreichbar (10.20.30.40)")), \
                mock.patch("catalytix.mailer.smtp_connect", side_effect=OSError("mail.geheimfirma.de refused")):
            files = self.package()
        checks = files["checks.json"]
        self.assertIn('"ok": false', checks)
        self.assertNotIn("geheimfirma", checks.lower())
        self.assertNotIn("10.20.30.40", checks)

    def test_cli_writes_into_data_folder(self):
        r = self.app.test_cli_runner().invoke(args=["diagnose", "--ohne-tests"])
        self.assertEqual(r.exit_code, 0, r.output)
        folder = Path(self.app.instance_path) / "diagnose"
        files = sorted(folder.glob("catalytix-diagnose-*.zip"))
        self.assertTrue(files)
        content = unpack(files[-1].read_bytes())
        self.assertIn("Konsole", content["https.json"])
        self.assertFalse([w for w in self.FORBIDDEN if w in "\n".join(content.values()).lower()])
        for f in files:
            f.unlink()

    def test_maintenance_tab(self):
        page = self.c.get("/admin/einstellungen?bereich=wartung").get_data(as_text=True)
        self.assertIn("Diagnosepaket herunterladen", page)
        self.assertNotIn('name="bereich" value="wartung">\n  <div class="grid', page)   # no settings form


class LogFileTests(unittest.TestCase):
    def test_log_written_to_data_folder_and_old_files_removed(self):
        folder = Path(tempfile.mkdtemp(prefix="catalytix-log-"))
        (folder / "logs").mkdir()
        old = folder / "logs" / f"catalytix-{date.today() - timedelta(days=40):%Y-%m-%d}.log"
        old.write_text("alt\n")
        app = create_app({"TESTING": True, "DIAG_LOG_FILE": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        try:
            diagnostics.install_log_file(folder)
            logging.getLogger("catalytix.test").warning("Hallo Protokoll")
            logging.getLogger("werkzeug").info('127.0.0.1 - - "GET /admin/benutzer?q=Muster HTTP/1.1" 200')
            lines = diagnostics.log_lines(folder, 1)
            self.assertTrue(any("Hallo Protokoll" in line for line in lines))
            self.assertFalse(any("q=Muster" in line for line in lines))     # no request lines
            self.assertFalse(old.exists())
        finally:
            diagnostics.uninstall_log_file(folder)
            diagnostics.uninstall_log_file(Path(app.instance_path))


class VersionHistoryTests(BaseCase):
    def test_recorded_once_per_version(self):
        set_setting("version_history", "")
        db.session.commit()
        diagnostics.record_version()
        diagnostics.record_version()
        hist = diagnostics.version_history()
        self.assertEqual(len(hist), 1)
        from catalytix import __version__
        self.assertEqual(hist[0]["version"], __version__)
        self.assertTrue(get_setting("version_history"))


class MaskTests(unittest.TestCase):
    def test_secrets_masked(self):
        for text in ("password=abc123", "Passwort: abc123", '"token": "abc123"', "Authorization: Bearer abc123",
                     "api_key=abc123", "enc:gAAAAABabc123abc123abc123abc123",
                     "https://www.googleapis.com/books/v1/volumes?q=x&key=abc123"):
            self.assertNotIn("abc123", diagnostics.mask_secrets(text), text)
        masked = diagnostics.mask_secrets("First-time setup pending – setup code: TZVM-WMPF-K8Z8  (asked for …)")
        self.assertNotIn("TZVM", masked)                       # whoever has it could take over the setup


if __name__ == "__main__":
    unittest.main()
