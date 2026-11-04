"""Automatic backup: create, verify, rotate, download, restore."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import backup, create_app  # noqa: E402
from catalytix.models import Book, User, db, get_setting, set_setting  # noqa: E402

T0 = datetime(2026, 10, 19, 2, 30)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bdir = self.tmp / "sicherungen"
        os.environ["CATALYTIX_BACKUP_DIR"] = str(self.bdir)
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                               "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp / 'catalytix.db'}"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        for name, role in (("admin", "admin"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local", email=f"{name}@example.org")
            u.set_password("geheim12345")
            db.session.add(u)
        db.session.add(Book(title="Faust"))
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        os.environ.pop("CATALYTIX_BACKUP_DIR", None)

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": "geheim12345"})

    def titles(self, path):
        con = sqlite3.connect(path)
        try:
            return [r[0] for r in con.execute("SELECT title FROM books ORDER BY id")]
        finally:
            con.close()

    def test_backup_is_complete_and_consistent(self):
        p = backup.create_backup("-manuell", T0)
        self.assertTrue(p.name.startswith("catalytix-2026-10-19_023000"))
        self.assertEqual(self.titles(p), ["Faust"])
        con = sqlite3.connect(p)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertFalse(list(self.bdir.glob("*.part")))

    def test_keeps_only_newest_n(self):
        set_setting("backup_keep", "10")
        db.session.commit()
        for d in range(14):
            backup.create_backup("", T0 + timedelta(days=d))
        names = [b["name"] for b in backup.list_backups()]
        self.assertEqual(len(names), 10)
        self.assertTrue(names[0].startswith("catalytix-2026-11-01"))   # newest
        self.assertTrue(names[-1].startswith("catalytix-2026-10-23"))  # oldest kept
        set_setting("backup_keep", "3")
        db.session.commit()
        backup.rotate()
        self.assertEqual(len(backup.list_backups()), 3)

    def test_default_is_ten(self):
        self.assertEqual(backup.keep_count(), 10)
        self.assertEqual(get_setting("backup_enabled"), "1")

    def test_daily_run_once_per_day_after_hour(self):
        self.assertIsNone(backup.run_daily(T0.replace(hour=1)))          # before 2 a.m.
        ok, msg = backup.run_daily(T0)
        self.assertTrue(ok, msg)
        self.assertIsNone(backup.run_daily(T0 + timedelta(hours=3)))     # already ran today
        self.assertTrue(backup.run_daily(T0 + timedelta(days=1))[0])
        self.assertEqual(len(backup.list_backups()), 2)
        set_setting("backup_enabled", "0")
        db.session.commit()
        self.assertIsNone(backup.run_daily(T0 + timedelta(days=2)))

    def test_failure_is_reported(self):
        with mock.patch.object(backup, "create_backup", side_effect=backup.BackupError("Platte voll")), \
                mock.patch.object(backup, "_notify_admins") as notify:
            ok, msg = backup.run_daily(T0)
        self.assertFalse(ok)
        notify.assert_called_once()
        self.assertIn("fehlgeschlagen", backup.warning())
        self.login("admin")
        self.assertIn("Datensicherung", self.c.get("/").get_data(as_text=True))
        backup.run_now("-manuell", T0 + timedelta(minutes=5))
        self.assertEqual(backup.warning(), "")

    def test_failure_mail_to_admins(self):
        from catalytix.mailer import render_mail
        with self.app.test_request_context():
            subject, text, html = render_mail("backup_failed", user=User.query.first(), message="Platte voll")
        self.assertIn("Datensicherung fehlgeschlagen", subject)
        self.assertIn("Platte voll", html)

    def test_admin_page_and_actions(self):
        self.login("admin")
        r = self.c.get("/admin/datensicherung")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Noch keine Sicherung", r.get_data(as_text=True))
        r = self.c.post("/admin/datensicherung/jetzt", follow_redirects=True)
        self.assertIn("gesichert", r.get_data(as_text=True))
        name = backup.list_backups()[0]["name"]
        dl = self.c.get(f"/admin/datensicherung/{name}")
        self.assertEqual(dl.status_code, 200)
        self.assertTrue(dl.data.startswith(b"SQLite format 3"))
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        self.c.post("/admin/datensicherung", data={"backup_enabled": "1", "backup_keep": "5", "backup_hour": "3"})
        self.assertEqual(get_setting("backup_keep"), "5")
        self.assertEqual(get_setting("backup_hour"), "3")
        self.c.post(f"/admin/datensicherung/{name}/loeschen")
        self.assertEqual(backup.list_backups(), [])

    def test_only_admins(self):
        backup.create_backup("-manuell", T0)
        name = backup.list_backups()[0]["name"]
        self.login("leser")
        for url in ["/admin/datensicherung", f"/admin/datensicherung/{name}"]:
            self.assertNotEqual(self.c.get(url).status_code, 200, url)
        self.assertNotEqual(self.c.post("/admin/datensicherung/jetzt").status_code, 200)

    def test_no_path_tricks(self):
        self.login("admin")
        (self.tmp / "geheim.db").write_text("x")
        for name in ["../catalytix.db", "..%2Fcatalytix.db", "geheim.db", "catalytix-2026-10-19_023000.db"]:
            self.assertEqual(self.c.get(f"/admin/datensicherung/{name}").status_code, 404, name)
        self.assertIsNone(backup.find_backup("../catalytix.db"))

    def test_restore_replaces_data_and_keeps_safety_copy(self):
        snap = backup.create_backup("-manuell", T0)
        db.session.add(Book(title="Woyzeck"))
        db.session.commit()
        safety = backup.restore_backup(snap.name)
        self.assertTrue(safety.name.endswith("-vor-wiederherstellung.db"))
        self.assertEqual(self.titles(self.tmp / "catalytix.db"), ["Faust"])
        self.assertEqual(self.titles(safety), ["Faust", "Woyzeck"])
        self.assertEqual([b.title for b in Book.query.order_by(Book.id)], ["Faust"])

    def test_restore_does_not_delete_the_backup_being_restored(self):
        set_setting("backup_keep", "2")
        db.session.commit()
        oldest = backup.create_backup("", T0)
        backup.create_backup("", T0 + timedelta(days=1))
        backup.restore_backup(oldest.name)
        self.assertEqual(self.titles(self.tmp / "catalytix.db"), ["Faust"])

    def test_cli(self):
        runner = self.app.test_cli_runner()
        r = runner.invoke(args=["backup"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("backed up", r.output)
        r = runner.invoke(args=["list-backups"])
        self.assertIn("catalytix-", r.output)
        name = backup.list_backups()[0]["name"]
        r = runner.invoke(args=["restore-backup", name, "--yes"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("has been restored", r.output)


class NotSqliteTests(unittest.TestCase):
    def test_in_memory_is_not_supported(self):
        app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        with app.app_context():
            db.create_all()
            self.assertFalse(backup.supported())
            self.assertIsNone(backup.run_daily(T0))
            u = User(username="admin", display_name="A", role="admin", auth_source="local")
            u.set_password("geheim12345")
            db.session.add(u)
            db.session.commit()
            c = app.test_client()
            c.post("/login", data={"username": "admin", "password": "geheim12345"})
            self.assertIn("nur für die eingebaute SQLite", c.get("/admin/datensicherung").get_data(as_text=True))
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
