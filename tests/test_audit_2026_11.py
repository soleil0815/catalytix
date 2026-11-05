"""3.0.2: findings of the monthly code audit (November 2026) – each test fails on 3.0.1."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_dbversion import Base as DbBase, set_stamp, stamp_of  # noqa: E402
from test_people import PW, Base  # noqa: E402
from test_update_sh import IMG, Base as UpdateBase, img  # noqa: E402

from catalytix import __version__, dbversion, mailer, retention  # noqa: E402
from catalytix.models import Book, Group, Loan, Setting, User, db, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- Data and versions

class StaleBackupMarkerTests(DbBase):
    def test_failed_upgrade_then_old_version_then_upgrade_backs_up_again(self):
        # Before: the "backup already made" marker of the failed attempt survived weeks on the old version –
        # the next upgrade made no new backup, update.sh then found none from this run
        self.app()
        set_stamp(self.db, "2.99.0")
        with mock.patch("catalytix._migrate_and_init", side_effect=RuntimeError("migration kaputt")):
            with self.assertRaises(RuntimeError):
                self.app()
        self.assertEqual(len(self.backups_named("-vor-")), 1)
        app = create_plain(self.db)
        with app.app_context():                       # back on the old version, it runs normally
            self.assertEqual(dbversion.prepare(db, "2.99.0"), "same")
            dbversion.stamp(db, "2.99.0")
            self.assertFalse(db.session.get(Setting, dbversion.BACKUP_KEY).value)
            db.session.remove()
            db.engine.dispose()
        time.sleep(1.1)                               # backup names carry the time in seconds
        self.app()
        self.assertEqual(len(self.backups_named("-vor-")), 2, self.backups_named())
        self.assertEqual(stamp_of(self.db), __version__)


def create_plain(path):
    """App object on that database WITHOUT the start-up steps (stands in for the old program version)."""
    from flask import Flask
    app = Flask("old")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{path}"
    db.init_app(app)
    return app


class DataTests(Base):
    def librarian_in(self, group):
        u = User(username="gbib", display_name="G", role="librarian", auth_source="local")
        u.set_password(PW)
        u.groups.append(group)
        db.session.add(u)
        db.session.commit()
        self.c.post("/logout")
        self.c.post("/login", data={"username": "gbib", "password": PW})
        return u

    def test_quick_add_merges_same_isbn(self):
        payload = {"title": "Faust II", "isbn": "9783161484100"}
        r1 = self.c.post("/api/buecher", json=payload)
        r2 = self.c.post("/api/buecher", json=payload)
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r2.get_json()["ok"])
        books = Book.query.filter_by(isbn="9783161484100").all()
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].copies, 2)
        self.assertTrue(r2.get_json()["merged"])

    def test_quick_add_other_group_is_a_new_book(self):
        self.c.post("/api/buecher", json={"title": "Faust II", "isbn": "9783161484100"})
        self.c.post("/api/buecher", json={"title": "Faust II", "isbn": "9783161484100", "group_id": self.group.id})
        self.assertEqual(Book.query.filter_by(isbn="9783161484100").count(), 2)

    def test_delete_user_never_removes_an_ongoing_loan(self):
        from datetime import date, timedelta
        leser = User.query.filter_by(username="leser").one()
        db.session.add(Loan(book=self.book, user=leser, due_at=date.today() + timedelta(days=7)))
        db.session.commit()
        db.session.expire_all()
        with self.assertRaises(retention.OpenLoans):
            retention.delete_user(leser)
        db.session.rollback()
        self.assertEqual(Loan.query.filter_by(user_id=leser.id, returned_at=None).count(), 1)
        self.assertIsNotNone(db.session.get(User, leser.id))

    def test_invisible_book_gives_404_not_403(self):
        other = Group(name="Andere")
        db.session.add(other)
        hidden = Book(title="Geheim", copies=1, group=other)
        db.session.add(hidden)
        db.session.commit()
        self.librarian_in(self.group)
        for url in (f"/buecher/{hidden.id}/bearbeiten", f"/buecher/{hidden.id}/loeschen",
                    f"/api/buecher/{hidden.id}/exemplar"):
            r = self.c.post(url) if "bearbeiten" not in url else self.c.get(url)
            self.assertEqual(r.status_code, 404, url)
        r = self.c.get("/buecher/999999/bearbeiten")
        self.assertEqual(r.status_code, 404)

    def test_visible_but_not_manageable_stays_403(self):
        other = Group(name="Andere")
        db.session.add(other)
        shown = Book(title="Sichtbar", copies=1, group=other)
        db.session.add(shown)
        set_setting("catalog_visibility", "all")
        db.session.commit()
        self.librarian_in(self.group)
        self.assertEqual(self.c.get(f"/buecher/{shown.id}/bearbeiten").status_code, 403)

    def test_covers_not_cached_by_shared_proxies(self):
        cover_dir = Path(self.app.config["COVER_DIR"])
        cover_dir.mkdir(parents=True, exist_ok=True)
        (cover_dir / "abc.jpg").write_bytes(b"\xff\xd8\xff")
        r = self.c.get("/covers/abc.jpg")
        self.assertEqual(r.status_code, 200)
        cc = r.headers.get("Cache-Control", "")
        self.assertIn("private", cc)
        self.assertNotIn("public", cc)


# ---------------------------------------------------------------- Account and mails

class EmailChangeTests(Base):
    def setUp(self):
        super().setUp()
        set_setting("app_base_url", "https://bib.firma.local")
        self.u = User(username="neu", display_name="Neu", role="reader", auth_source="local",
                      self_registered=True, email="alt@firma.local", email_verified=True)
        self.u.set_password(PW)
        db.session.add(self.u)
        db.session.commit()

    def link_for(self, new):
        from catalytix.views import register
        with self.app.test_request_context(), \
                mock.patch("catalytix.mailer.mail_active", return_value=True), \
                mock.patch("catalytix.mailer.send_mail", return_value=(True, "")), \
                mock.patch("catalytix.mailer.render_mail", return_value=("s", "t", "h")) as rm:
            self.assertTrue(register.send_email_change(db.session.get(User, self.u.id), new))
        return rm.call_args.kwargs["link"].replace("https://bib.firma.local", "")

    def email(self):
        db.session.expire_all()
        return db.session.get(User, self.u.id).email

    def test_stale_and_used_links_are_invalid(self):
        typo = self.link_for("tipfehler@firma.local")
        good = self.link_for("neu@firma.local")
        self.c.get(good)
        self.assertEqual(self.email(), "neu@firma.local")
        self.assertEqual(self.c.get(typo).status_code, 404)        # before: switched the account to the typo
        self.assertEqual(self.email(), "neu@firma.local")
        self.assertEqual(self.c.get(good).status_code, 404)        # used once
        self.assertEqual(self.email(), "neu@firma.local")

    def test_domain_checked_again_on_confirm(self):
        link = self.link_for("x@extern.example")
        set_setting("registration_domains", "firma.local")
        db.session.commit()
        self.c.get(link)
        self.assertEqual(self.email(), "alt@firma.local")


class MailLinkTests(Base):
    def test_protocol_relative_links_never_in_mails(self):
        set_setting("app_base_url", "https://bib.firma.local")
        for value, expect in (("//evil.example/x", ""), ("/\\evil.example/x", ""),
                              ("/datenschutz", "https://bib.firma.local/datenschutz"),
                              ("https://firma.local/impressum", "https://firma.local/impressum")):
            set_setting("imprint_url", value)
            with self.app.test_request_context():
                self.assertEqual(mailer._ctx()["imprint_url"], expect, value)
        set_setting("app_base_url", "")
        set_setting("imprint_url", "/datenschutz")
        with self.app.test_request_context():
            self.assertEqual(mailer._ctx()["imprint_url"], "")     # no base address: no broken relative link


class TwoFactorNameTests(Base):
    def test_issuer_in_english_for_default_name(self):
        from catalytix import i18n, twofa
        with self.app.test_request_context(), i18n.force_locale("en"):
            self.assertIn("issuer=Library", twofa.otpauth_uri("ABCD", "anna"))
            self.assertEqual(twofa._rp_name(), "Library")


# ---------------------------------------------------------------- Update path

class UpdatePathTests(UpdateBase):
    def test_unwritable_env_stops_before_the_database_is_touched(self):
        self.installed("3.1.0")
        self.publish("3.0.0")
        env = self.inst / ".env"
        if os.geteuid() != 0:
            env.chmod(0o444)
        elif subprocess.run(["mount", "--bind", str(env), str(env)], capture_output=True).returncode == 0:
            self.addCleanup(subprocess.run, ["umount", str(env)], capture_output=True)
            if subprocess.run(["mount", "-o", "remount,ro,bind", str(env)], capture_output=True).returncode:
                self.skipTest("cannot make .env read-only")
        else:
            self.skipTest("cannot make .env read-only as root")
        r = self.run_update("-y", "--version=3.0.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Cannot write .env", self.out)
        self.assertNotIn("restore-for-version", self.docker_log())
        self.assertEqual(self.state["container"]["status"], "running")

    def test_env_read_like_compose(self):
        for line in ("export CATALYTIX_TAG=3.0.0\n", "CATALYTIX_TAG=3.0.0  # pinned\n"):
            with self.subTest(line=line):
                self.setUp()
                self.installed("3.0.0", env=line)
                self.state["registry"][f"{IMG}:3.0.0"] = img("3.0.0")
                self.publish("3.1.0")
                self.run_update()
                log = self.docker_log()
                self.assertIn(f"docker pull {IMG}:3.0.0", log)
                self.assertNotIn(f"docker pull {IMG}:latest", log)

    def test_other_image_started_is_not_reported_as_success(self):
        self.installed("3.0.0")
        self.state["images"][f"{IMG}:3.0.0"] = img("3.0.0")
        self.publish("3.1.0")
        before = (self.inst / "update.sh").read_text()
        r = self.run_update(env={"CATALYTIX_TAG": "3.0.0"})        # exported in the shell: wins over .env
        self.assertNotEqual(r.returncode, 0, self.out)
        self.assertNotIn("Version 3.1.0 is running", self.out)
        self.assertIn("another image", self.out)
        self.assertEqual((self.inst / "update.sh").read_text(), before)

    def test_remove_orphans_only_for_this_folders_project(self):
        self.installed("3.0.0")
        self.state["container"].update(project="stack", working_dir="/opt/other-stack")
        self.publish("3.1.0")
        self.run_update()
        self.assertIn("-p stack up -d catalytix", self.docker_log())
        self.assertNotIn("--remove-orphans", self.docker_log())
        self.log.unlink()
        self.state["container"].update(project="bib", working_dir=str(self.inst))
        self.publish("3.1.1")
        self.run_update()
        self.assertIn("-p bib up -d --remove-orphans catalytix", self.docker_log())

    def test_temporary_container_removed_with_its_volume(self):
        self.installed("3.0.0")
        self.publish("3.1.0")
        self.run_update()
        self.assertRegex(self.docker_log(), r"(?m)^docker rm -f -v ")

    def test_cleanup_refuses_names_with_line_break_in_data(self):
        self.installed("3.0.0")
        i = self.inst
        (i / "catalytix").mkdir()
        (i / "catalytix" / "__init__.py").write_text('__version__ = "2.20.1"\n')
        (i / "docs").mkdir()
        (i / "docs" / "keep.txt").write_text("K")
        (i / "data" / "x\nb").symlink_to(i / "docs")     # protects docs – but only if the name can be read
        r = self.run_update("--cleanup", "-y")
        self.assertNotEqual(r.returncode, 0, self.out)
        self.assertIn("line break", self.out)
        self.assertTrue((i / "docs" / "keep.txt").exists())
        self.assertTrue((i / "catalytix").exists())


# ---------------------------------------------------------------- Build chain

class BuildChainTests(unittest.TestCase):
    def fake_gh(self, tmp, out):
        f = Path(tmp) / "gh"
        f.write_text(f"#!/bin/sh\nprintf '%s\\n' '{out}'\n")
        f.chmod(0o755)

    def test_unreadable_release_list_is_not_first_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fake_gh(tmp, "something-else")
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GITHUB_REPOSITORY="o/r")
            r = subprocess.run(["bash", "-c", "set -e; . tools/ci.sh; h=$(highest_release); echo \"h=[$h]\""],
                               cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("h=[]", r.stdout)
            self.fake_gh(tmp, "")                                    # really no release yet: empty, no error
            r = subprocess.run(["bash", "-c", "set -e; . tools/ci.sh; h=$(highest_release); echo \"h=[$h]\""],
                               cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("h=[]", r.stdout)

    def test_changelog_needs_both_files(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import security_update
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "old").write_text("deb a 1\n")
            (d / "new").write_text("deb a 2\n")
            de = d / "CHANGELOG.md"
            de.write_text("# Änderungen\n\n## 3.0.1 – 01.11.2026\n")
            with mock.patch.object(security_update, "CHANGELOG", de), \
                    mock.patch.object(security_update, "CHANGELOG_EN", d / "CHANGELOG.en.md"), \
                    mock.patch("sys.stderr"):
                rc = security_update.main(["changelog", "3.0.1-s1", "3.0.1", str(d / "old"), str(d / "new")])
            self.assertNotEqual(rc, 0)
            self.assertNotIn("3.0.1-s1", de.read_text())

    def test_package_list_step_stops_on_errors(self):
        text = (ROOT / "Dockerfile").read_text()
        i = text.index("> /app/image-packages.txt")
        block = text[text.rindex("RUN ", 0, i):text.index("\n\n", i)]
        self.assertIn("set -eu", block)
        self.assertNotIn("| sort", block)                           # a pipe would hide a failing dpkg-query
        self.assertNotIn("\n    ! ", block)                         # "! cmd" is ignored by set -e


# ---------------------------------------------------------------- Interface

class InterfaceTests(unittest.TestCase):
    css = (ROOT / "catalytix" / "static" / "css" / "app.css").read_text()

    def test_table_cells_do_not_break_one_letter_per_line(self):
        rule = re.search(r"(?m)^([^{}\n]*)\{ overflow-wrap: anywhere; \}", self.css)
        self.assertNotIn("td", [s.strip() for s in rule.group(1).split(",")])
        self.assertRegex(self.css, r"(?m)^td \{ overflow-wrap: break-word; \}")

    def test_closed_phone_sidebar_not_reachable_with_tab(self):
        m = re.search(r"\.sidebar \{ position: fixed;[^}]*\}", self.css)
        self.assertIn("visibility: hidden", m.group(0))
        self.assertRegex(self.css, r"body\.nav-open \.sidebar \{[^}]*visibility: visible")
        base = (ROOT / "catalytix" / "templates" / "base.html").read_text()
        self.assertIn('aria-controls="sidebar" aria-expanded="false"', base)
        self.assertIn("aria-expanded", (ROOT / "catalytix" / "static" / "js" / "app.js").read_text())

    def test_icon_only_buttons_have_a_name(self):
        base = (ROOT / "catalytix" / "templates" / "base.html").read_text()
        self.assertRegex(base, r"url_for\('books.create'\) \}\}\" aria-label=")
        desk = (ROOT / "catalytix" / "templates" / "loans" / "desk.html").read_text()
        for m in re.finditer(r"<button[^>]*>\{\{ icon\(\"camera\"\) \}\}</button>", desk):
            self.assertIn("aria-label=", m.group(0))

    def test_muted_text_contrast(self):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
        muted = re.search(r"--muted: (#[0-9a-f]{6});", self.css).group(1)
        for bg in ("#fffdf8", "#ece5d6", "#f5f0e6"):
            hi, lo = sorted((lum(muted), lum(bg)), reverse=True)
            self.assertGreaterEqual((hi + .05) / (lo + .05), 4.5, bg)
        self.assertIn(".badge.brass { background: var(--brass-soft); color: var(--brass-text); }", self.css)


if __name__ == "__main__":
    unittest.main()
