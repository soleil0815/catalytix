"""3.0.0: Datenbank-Version beim Start (Sicherung vor dem Versionswechsel, Abbruch bei neuerer Datenbank),
Zurückspielen für update.sh (restore-before / restore-for-version) und Versionsprüfung der docker-compose.yml."""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import __version__, composefile, create_app, dbversion  # noqa: E402
from catalytix.models import Setting, db, get_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def stamp_of(path):
    con = sqlite3.connect(path)
    try:
        row = con.execute('SELECT value FROM settings WHERE "key" = ?', ("db_version",)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def set_stamp(path, value):
    con = sqlite3.connect(path)
    try:
        if value is None:
            con.execute('DELETE FROM settings WHERE "key" = ?', ("db_version",))
        else:
            con.execute('UPDATE settings SET value = ? WHERE "key" = ?', (value, "db_version"))
        con.commit()
    finally:
        con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cx-dbv-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "catalytix.db"
        self.backups = self.tmp / "backups"
        patcher = mock.patch.dict(os.environ, {"CATALYTIX_BACKUP_DIR": str(self.backups),
                                               "CATALYTIX_SCHEDULER": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(dbversion.SKIP_ENV, None)

    def app(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "k", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.db}"})
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        return app

    def backups_named(self, pattern=".*"):
        if not self.backups.exists():
            return []
        return sorted(p.name for p in self.backups.iterdir() if re.search(pattern, p.name))


class StartupTests(Base):
    def test_new_database_gets_version_without_backup(self):
        self.app()
        self.assertEqual(stamp_of(self.db), __version__)
        self.assertEqual(self.backups_named(), [])

    def test_database_from_before_3_is_backed_up_first(self):
        self.app()
        set_stamp(self.db, None)                     # wie eine Datenbank von 2.x
        self.app()
        names = self.backups_named(rf"-vor-{re.escape(__version__)}\.db$")
        self.assertEqual(len(names), 1, self.backups_named())
        self.assertIsNone(stamp_of(self.backups / names[0]))       # Stand VOR der Umstellung
        self.assertEqual(stamp_of(self.db), __version__)

    def test_older_version_backed_up_once(self):
        self.app()
        set_stamp(self.db, "2.99.0")
        self.app()
        self.app()                                   # gleicher Stand: keine weitere Sicherung
        self.assertEqual(len(self.backups_named("-vor-")), 1)
        self.assertEqual(stamp_of(self.backups / self.backups_named("-vor-")[0]), "2.99.0")

    def test_newer_database_refuses_to_start_and_changes_nothing(self):
        self.app()
        set_stamp(self.db, "99.0.0")
        before = self.db.read_bytes()
        with self.assertRaises(SystemExit) as cm:
            self.app()
        msg = str(cm.exception)
        self.assertIn("99.0.0", msg)
        self.assertIn(__version__, msg)
        self.assertIn("vor-99.0.0", msg)                             # welche Sicherung zurückzuspielen ist
        self.assertIn("CATALYTIX_TAG=99.0.0", msg)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.backups_named(), [])

    def test_backup_failure_stops_the_start(self):
        self.app()
        set_stamp(self.db, "2.99.0")
        from catalytix.backup import BackupError
        with mock.patch("catalytix.backup.create_backup", side_effect=BackupError("Platte voll")):
            with self.assertRaises(SystemExit) as cm:
                self.app()
        self.assertIn("not starting", str(cm.exception))
        self.assertEqual(stamp_of(self.db), "2.99.0")

    def test_other_databases_get_a_hint_instead_of_a_backup(self):
        self.app()
        set_stamp(self.db, "2.99.0")
        with mock.patch("catalytix.backup.supported", return_value=False), \
                self.assertLogs("catalytix.dbversion", "WARNING") as logs:
            self.app()
        self.assertIn("mysqldump", "\n".join(logs.output))
        self.assertEqual(stamp_of(self.db), __version__)

    def test_skip_check_runs_no_migrations(self):
        self.app()
        set_stamp(self.db, "99.0.0")
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            self.app()
        self.assertEqual(stamp_of(self.db), "99.0.0")


class RestoreCommandTests(Base):
    def cli(self, *args):
        app = create_app({"TESTING": True, "SECRET_KEY": "k", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.db}"})
        res = app.test_cli_runner().invoke(args=list(args))
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        return res

    def prepare_update(self, old="3.0.0", new="3.1.0"):
        """Datenbank von <old>, beim Start von <new> gesichert, danach von <new> geändert."""
        self.app()
        set_stamp(self.db, old)
        from catalytix.backup import create_backup
        app = self.app()
        with app.app_context():
            create_backup(f"-vor-{new}", rotate_after=False)
        set_stamp(self.db, new)

    def test_restore_before_puts_back_the_backup_of_this_run(self):
        self.prepare_update()
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            res = self.cli("restore-before", "3.1.0", "--since", str(int(time.time()) - 60), "--yes")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(stamp_of(self.db), "3.0.0")

    def test_restore_before_ignores_older_attempts(self):
        self.prepare_update()
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            res = self.cli("restore-before", "3.1.0", "--since", str(int(time.time()) + 3600), "--yes")
        self.assertEqual(res.exit_code, 3, res.output)
        self.assertEqual(stamp_of(self.db), "3.1.0")                  # nichts überschrieben

    def test_restore_for_version_takes_newest_usable_backup(self):
        self.prepare_update()                                         # vor-3.1.0 enthält Stand 3.0.0
        from catalytix.backup import create_backup
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            app = self.app()
            with app.app_context():
                time.sleep(1.1)
                create_backup("-manuell", rotate_after=False)         # später, aber von 3.1.0
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            res = self.cli("restore-for-version", "3.0.0", "--yes")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("-vor-3.1.0", res.output)
        self.assertEqual(stamp_of(self.db), "3.0.0")
        with mock.patch.dict(os.environ, {dbversion.SKIP_ENV: "1"}):
            res = self.cli("restore-for-version", "1.0.0", "--yes")
        self.assertEqual(res.exit_code, 3, res.output)

    def test_update_backups_survive_rotation(self):
        from catalytix.backup import create_backup, list_backups, rotate
        from datetime import datetime, timedelta
        app = self.app()
        with app.app_context():
            t = datetime(2026, 1, 1)
            for i in range(4):
                create_backup(f"-vor-3.{i}.0", now=t + timedelta(minutes=i), rotate_after=False)
            for i in range(5):
                create_backup("", now=t + timedelta(hours=1, minutes=i), rotate_after=False)
            rotate(keep=2)
            kinds = [(b["kind"], b["before_version"]) for b in list_backups()]
        self.assertEqual(sum(1 for k, v in kinds if v), 3)            # die neuesten 3 „vor Update“
        self.assertEqual(sum(1 for k, v in kinds if not v), 2)
        self.assertIn(("vor Update", "3.3.0"), kinds)


class ComposeFileTests(unittest.TestCase):
    def test_not_checked_outside_the_container(self):
        with mock.patch.dict(os.environ, {"CATALYTIX_COMPOSE": ""}, clear=False):
            os.environ.pop("CATALYTIX_IN_CONTAINER", None)
            self.assertIsNone(composefile.outdated())

    def test_versions(self):
        with mock.patch.dict(os.environ, {"CATALYTIX_IN_CONTAINER": "1"}):
            os.environ.pop("CATALYTIX_COMPOSE", None)
            self.assertEqual(composefile.outdated(), 0)                # alte Datei ohne Angabe
            os.environ["CATALYTIX_COMPOSE"] = str(composefile.COMPOSE_MIN - 1)
            self.assertEqual(composefile.outdated(), composefile.COMPOSE_MIN - 1)
            os.environ["CATALYTIX_COMPOSE"] = str(composefile.COMPOSE_MIN)
            self.assertIsNone(composefile.outdated())
            os.environ["CATALYTIX_COMPOSE"] = "kaputt"
            self.assertEqual(composefile.outdated(), 0)

    def test_numbers_match_everywhere(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        m = re.search(r'^\s*CATALYTIX_COMPOSE: "(\d+)"', compose, re.M)
        self.assertEqual(int(m.group(1)), composefile.COMPOSE_VERSION)
        self.assertIn(f'catalytix.compose="{composefile.COMPOSE_VERSION}"', (ROOT / "Dockerfile").read_text())
        self.assertLessEqual(composefile.COMPOSE_MIN, composefile.COMPOSE_VERSION)

    def test_compose_file_is_stable(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("build:", compose)
        self.assertIn('image: "${CATALYTIX_IMAGE:-ghcr.io/soleil0815/catalytix}:${CATALYTIX_TAG:-latest}"', compose)
        self.assertNotIn(__version__, compose)                          # ändert sich nicht mit jeder Version
        self.assertNotIn("version:", compose)                           # auch docker-compose 1.27+ liest sie

    def test_image_brings_only_host_files(self):
        df = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY docker-compose.yml .env.example update.sh /app/dist/", df)
        self.assertIn("CATALYTIX_IN_CONTAINER=1", df)

    def test_admin_sees_hint(self):
        from test_people import PW, Base as PeopleBase  # noqa: F401
        app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        from catalytix.models import User
        with app.app_context():
            db.create_all()
            for name, role in (("chef", "admin"), ("leser", "reader")):
                u = User(username=name, display_name=name, role=role, auth_source="local")
                u.set_password("geheim12345")
                db.session.add(u)
            from catalytix.models import set_setting
            set_setting("setup_completed", "1")
            db.session.commit()
            c = app.test_client()
            with mock.patch.dict(os.environ, {"CATALYTIX_IN_CONTAINER": "1", "CATALYTIX_COMPOSE": "1"}):
                c.post("/login", data={"username": "chef", "password": "geheim12345"})
                self.assertIn("docker-compose.yml auf dem Server ist veraltet", c.get("/").get_data(as_text=True))
                c.post("/logout")
                c.post("/login", data={"username": "leser", "password": "geheim12345"})
                self.assertNotIn("docker-compose.yml", c.get("/").get_data(as_text=True))
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
