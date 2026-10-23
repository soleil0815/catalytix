"""Befunde aus dem zweiten Code Review."""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_fix_auth import EXT, LAN, PW, Base  # noqa: E402

import catalytix  # noqa: E402
from catalytix import backup  # noqa: E402
from catalytix.models import Book, Group, User, db, get_setting, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class AdminNeedsSecondFactorTests(Base):
    def login_skipped_admin(self):
        self.settings(mfa_internal="skip")
        secret = self.give_totp("admin")
        r = self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)
        self.assertEqual(r.headers["Location"], "/")
        return secret

    def test_skipped_session_cannot_change_security_settings(self):
        self.login_skipped_admin()
        admin = self.user("admin")
        r = self.c.post(f"/admin/benutzer/{admin.id}/2fa-zuruecksetzen", environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])           # direkt zur Bestätigung
        self.assertTrue(self.user("admin").totp_enabled)          # nicht zurückgesetzt
        r = self.c.post("/admin/einstellungen", data={"mfa_internal": "same"}, environ_base=LAN)
        self.assertIn("/login/2fa", r.headers["Location"])
        self.assertEqual(get_setting("mfa_internal"), "skip")
        self.assertEqual(self.c.get("/admin/benutzer", environ_base=LAN).status_code, 200)   # ansehen geht

    def test_after_confirming_returns_to_page(self):
        from catalytix import twofa
        secret = self.login_skipped_admin()
        self.c.post("/admin/einstellungen", data={"mfa_internal": "same"}, environ_base=LAN)
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=LAN)
        self.assertEqual(r.headers["Location"], "/admin/einstellungen")
        self.c.post("/admin/einstellungen", data={"mfa_internal": "same"}, environ_base=LAN)
        self.assertEqual(get_setting("mfa_internal"), "same")

    def test_branding_and_groups_need_no_confirmation(self):
        self.login_skipped_admin()
        r = self.c.post("/admin/erscheinungsbild", data={"action": "save", "brand_accent": "#aa3366"}, environ_base=LAN)
        self.assertNotIn("/login/2fa", r.headers.get("Location", ""))
        self.assertEqual(get_setting("brand_accent"), "#aa3366")
        self.c.post("/admin/gruppen/neu", data={"name": "Y"}, environ_base=LAN)
        self.assertEqual(Group.query.filter_by(name="Y").count(), 1)

    def test_admin_without_2fa_unaffected(self):
        self.settings(mfa_internal="skip")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)
        r = self.c.post("/admin/gruppen/neu", data={"name": "X"}, environ_base=LAN)
        self.assertEqual(Group.query.filter_by(name="X").count(), 1, r.headers.get("Location"))


class SetupLockTests(Base):
    def test_existing_users_without_admin_and_marker_cannot_use_setup(self):
        db.session.delete(self.user("admin"))
        from catalytix.models import Setting
        Setting.query.filter_by(key="setup_completed").delete()
        db.session.commit()
        self.app.config.pop("_setup_done", None)
        r = self.c.get("/login")
        self.assertNotIn("/setup", r.headers.get("Location", ""))
        r = self.c.post("/setup", data={"username": "evil", "password": "x" * 12, "password2": "x" * 12})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(User.query.filter_by(username="evil").first())


class StepUpNotKilledByThrottleTests(Base):
    def test_throttled_account_keeps_pending_login(self):
        from catalytix import lockout, twofa
        secret = self.give_totp("admin")
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=EXT)
        for i in range(lockout.USER_MAX_FAILS):
            lockout.fail(f"x|198.51.100.{i}", "admin")
        r = self.c.post("/login/2fa", data={"code": twofa.totp_now(secret)}, environ_base=EXT)
        self.assertEqual(r.status_code, 429)
        with self.c.session_transaction() as s:
            self.assertIn("mfa_pending", s)


class GroupNameTests(Base):
    def test_umlaut_duplicate_is_message_not_500(self):
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)
        self.c.post("/admin/gruppen/neu", data={"name": "Ärzte"})
        r = self.c.post("/admin/gruppen/neu", data={"name": "ärzte"})
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/admin/gruppen/neu", data={"name": "Ärzte"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Group.query.count(), 1)


class DeskSignatureTests(Base):
    def test_non_ascii_signature_found(self):
        from catalytix.views.loans import _find_books
        db.session.add(Book(title="Medizin", signature="Ü-12", copies=1))
        db.session.commit()
        self.c.post("/login", data={"username": "admin", "password": PW}, environ_base=LAN)
        with self.app.test_request_context("/"):
            from flask_login import login_user
            login_user(self.user("admin"))
            self.assertEqual([b.title for b in _find_books("Ü-12")], ["Medizin"])


class BackupOrderTests(unittest.TestCase):
    def test_same_second_newest_kept_and_no_500(self):
        d = Path(tempfile.mkdtemp())
        app = catalytix.create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{d}/t.db"})
        with app.app_context():
            db.create_all()
            set_setting("backup_keep", "1")
            db.session.commit()
            with mock.patch.dict(os.environ, {"CATALYTIX_BACKUP_DIR": str(d / "b")}):
                now = datetime(2026, 10, 22, 10, 0, 0)
                ok1, _m = backup.run_now("-manuell", now)
                ok2, _m = backup.run_now("-manuell", now)
                names = [b["name"] for b in backup.list_backups()]
        self.assertTrue(ok1 and ok2)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("-manuell-1.db"), names)   # die neuere bleibt


class StartupTests(unittest.TestCase):
    def test_mysql_read_committed(self):
        self.assertEqual(catalytix._engine_options("mysql+pymysql://x")["isolation_level"], "READ COMMITTED")
        self.assertNotIn("isolation_level", catalytix._engine_options(""))
        self.assertNotIn("isolation_level", catalytix._engine_options("postgresql://x"))

    def test_external_db_ignores_leftover_sqlite_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "catalytix-alt.db").write_bytes(b"x" * 100)
        env = {"CATALYTIX_DATA_DIR": str(d), "DATABASE_URL": f"sqlite:///{d}/extern.db"}
        with mock.patch.dict(os.environ, env):
            app = catalytix.create_app({"TESTING": True})
        self.assertIn("extern.db", app.config["SQLALCHEMY_DATABASE_URI"])

    def test_env_key_whitespace_kept(self):
        with mock.patch.dict(os.environ, {"CATALYTIX_SECRET_KEY": " abc "}):
            self.assertEqual(catalytix._secret_key(Path(tempfile.mkdtemp())), " abc ")

    def test_refused_start_exits_with_code_4(self):
        d = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CATALYTIX_", "DATABASE_URL"))}
        env.update(CATALYTIX_DATA_DIR=d, CATALYTIX_SCHEDULER="0")
        subprocess.run([sys.executable, "-c", "import wsgi"], cwd=ROOT, env=env, check=True, capture_output=True)
        env["CATALYTIX_SECRET_KEY"] = "ein-ganz-anderer-schluessel"
        r = subprocess.run([sys.executable, "-c", "import wsgi"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr[-500:])


class UpdateScriptTests(unittest.TestCase):
    def test_no_tty_without_yes_aborts_questions(self):
        text = (ROOT / "update.sh").read_text()
        self.assertIn('[ -t 0 ] || {', text)
        self.assertIn("com.docker.compose.project", text)


if __name__ == "__main__":
    unittest.main()
