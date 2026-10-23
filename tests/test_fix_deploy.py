"""Betrieb/Deployment: Startsperre, Schlüssel, Schema-Nachrüstung, update.sh, Entrypoint, Zeitzone."""
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

import catalytix  # noqa: E402
from catalytix import _ensure_timezone, _secret_key, _startup_lock, create_app  # noqa: E402
from catalytix.models import Book, Group, Setting, User, _add_column_ddl, db, ensure_columns, get_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAS_FORK = "fork" in mp.get_all_start_methods()


# ------------------------------------------------------------------ Hilfen für Mehrprozess-Tests
def _start_app(data_dir, barrier, q):
    os.environ["CATALYTIX_DATA_DIR"] = data_dir
    os.environ["CATALYTIX_SCHEDULER"] = "0"
    os.environ.pop("CATALYTIX_SECRET_KEY", None)
    os.environ.pop("DATABASE_URL", None)
    barrier.wait()
    try:
        app = create_app()
        q.put(("ok", app.config["SECRET_KEY"]))
    except BaseException as exc:  # noqa: BLE001
        q.put(("err", f"{type(exc).__name__}: {str(exc)[:200]}"))


def _read_key(data_dir, barrier, q):
    barrier.wait()
    try:
        q.put(("ok", _secret_key(Path(data_dir))))
    except BaseException as exc:  # noqa: BLE001
        q.put(("err", repr(exc)))


def _run_pair(target, data_dir):
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(2), ctx.Queue()
    ps = [ctx.Process(target=target, args=(data_dir, barrier, q)) for _ in range(2)]
    for p in ps:
        p.start()
    res = [q.get(timeout=60) for _ in ps]
    for p in ps:
        p.join(30)
    return res


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-deploy-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ 1. Startsperre / gleichzeitiger Start
@unittest.skipUnless(HAS_FORK, "braucht fork")
class StartupRaceTest(TmpDir):
    def test_two_workers_start_on_empty_data_dir(self):
        for i in range(5):
            d = os.path.join(self.tmp, f"d{i}")
            os.mkdir(d)
            res = _run_pair(_start_app, d)
            self.assertEqual([r for r in res if r[0] != "ok"], [], res)
            key = Path(d, "secret_key").read_text().strip()
            self.assertTrue(key)
            self.assertEqual({r[1] for r in res}, {key})

    def test_lock_is_exclusive(self):
        import fcntl
        inst = Path(self.tmp)
        with _startup_lock(inst):
            with open(inst / ".startup.lock", "a+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(inst / ".startup.lock", "a+b") as other:   # nach dem Block wieder frei
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)

    def test_lock_unavailable_is_not_fatal(self):
        with _startup_lock(Path(self.tmp) / "gibt-es-nicht"):
            pass


class EnsureColumnsTolerantTest(TmpDir):
    def test_duplicate_column_from_other_process_is_skipped(self):
        """Hat ein anderer Prozess die Spalte inzwischen angelegt, bricht der Start nicht ab."""
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp}/x.db"})
        with app.app_context():
            import sqlalchemy
            real_inspect = sqlalchemy.inspect

            class StaleInspector:
                def __init__(self, insp):
                    self.insp = insp

                def has_table(self, name):
                    return self.insp.has_table(name)

                def get_columns(self, name):   # Stand „vor“ dem anderen Prozess: users.language fehlt
                    cols = self.insp.get_columns(name)
                    return [c for c in cols if not (name == "users" and c["name"] == "language")]

            with mock.patch("sqlalchemy.inspect", lambda e: StaleInspector(real_inspect(e))):
                self.assertEqual(ensure_columns(), [])
            db.session.remove()
            db.engine.dispose()

    def test_missing_column_is_added(self):
        uri = f"sqlite:///{self.tmp}/y.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            with db.engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users DROP COLUMN language")
            self.assertEqual(ensure_columns(), ["users.language"])
            db.session.remove()
            db.engine.dispose()


# ------------------------------------------------------------------ 2. secret_key
class SecretKeyTest(TmpDir):
    def test_concurrent_creation_same_key(self):
        if not HAS_FORK:
            self.skipTest("braucht fork")
        with mock.patch.dict(os.environ, {"CATALYTIX_SECRET_KEY": ""}):
            for i in range(30):
                d = os.path.join(self.tmp, f"k{i}")
                os.mkdir(d)
                res = _run_pair(_read_key, d)
                key = Path(d, "secret_key").read_text().strip()
                self.assertTrue(key)
                self.assertEqual(res, [("ok", key), ("ok", key)])

    def test_file_mode_0600(self):
        with mock.patch.dict(os.environ, {"CATALYTIX_SECRET_KEY": ""}):
            _secret_key(Path(self.tmp))
        self.assertEqual(os.stat(Path(self.tmp, "secret_key")).st_mode & 0o777, 0o600)

    def test_empty_key_file_is_never_used(self):
        Path(self.tmp, "secret_key").write_text("  \n")
        with mock.patch.dict(os.environ, {"CATALYTIX_SECRET_KEY": "   "}), \
                mock.patch.object(catalytix.time, "sleep"):
            with self.assertRaises(SystemExit) as cm:
                _secret_key(Path(self.tmp))
        self.assertIn("is empty", str(cm.exception))

    def test_waits_for_other_writer(self):
        f = Path(self.tmp, "secret_key")
        f.write_text("")
        calls = []

        def fake_sleep(_):
            calls.append(1)
            if len(calls) == 3:
                f.write_text("abc123\n")
        with mock.patch.dict(os.environ, {"CATALYTIX_SECRET_KEY": ""}), \
                mock.patch.object(catalytix.time, "sleep", fake_sleep):
            self.assertEqual(_secret_key(Path(self.tmp)), "abc123")


# ------------------------------------------------------------------ 6. Schlüssel-Prüfwert
class KeyMarkerTest(TmpDir):
    def _app(self, key):
        return create_app({"TESTING": True, "SECRET_KEY": key,
                           "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp}/k.db"})

    def _dispose(self, app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_changed_key_refuses_start_unless_accepted(self):
        app = self._app("schluessel-a")
        with app.app_context():
            marker = get_setting("secret_key_check")
            self.assertTrue(marker)
            self.assertNotIn("schluessel-a", marker)
        self._dispose(app)
        self._dispose(self._app("schluessel-a"))          # gleicher Schlüssel: startet
        with mock.patch.dict(os.environ, {"CATALYTIX_ACCEPT_NEW_KEY": ""}):
            with self.assertRaises(SystemExit) as cm:
                self._app("schluessel-b")
        self.assertIn("does NOT match", str(cm.exception))
        self.assertIn("CATALYTIX_ACCEPT_NEW_KEY", str(cm.exception))
        with mock.patch.dict(os.environ, {"CATALYTIX_ACCEPT_NEW_KEY": "1"}):
            app = self._app("schluessel-b")
        with app.app_context():
            self.assertNotEqual(get_setting("secret_key_check"), marker)
        self._dispose(app)
        self._dispose(self._app("schluessel-b"))          # danach ohne Variable
        with self.assertRaises(SystemExit):
            self._app("schluessel-a")

    def test_existing_db_without_marker_gets_one(self):
        app = self._app("k1")
        with app.app_context():
            db.session.delete(db.session.get(Setting, "secret_key_check"))
            db.session.commit()
        self._dispose(app)
        app = self._app("k1")
        with app.app_context():
            self.assertTrue(get_setting("secret_key_check"))
        self._dispose(app)


# ------------------------------------------------------------------ 7. DDL für ensure_columns
class AddColumnDDLTest(unittest.TestCase):
    def ddl(self, dialect, col):
        return _add_column_ddl(col.table, col, dialect)

    def test_reserved_table_names_are_quoted(self):
        from sqlalchemy.dialects import mysql, sqlite
        col = Group.__table__.c.name
        self.assertTrue(self.ddl(mysql.dialect(), col)[0].startswith("ALTER TABLE `groups` ADD COLUMN name "))
        # „key“ ist in MySQL reserviert
        k = Setting.__table__.c.key
        self.assertIn("ADD COLUMN `key`", self.ddl(mysql.dialect(), k)[0])
        self.assertTrue(self.ddl(sqlite.dialect(), User.__table__.c.language)[0]
                        .startswith("ALTER TABLE users ADD COLUMN language VARCHAR"))

    def test_mysql_text_column_without_default(self):
        from sqlalchemy.dialects import mysql
        stmts = self.ddl(mysql.dialect(), User.__table__.c.totp_secret)
        self.assertEqual(stmts[0], "ALTER TABLE users ADD COLUMN totp_secret TEXT")
        self.assertEqual(stmts[1], "UPDATE users SET totp_secret = ''")
        # VARCHAR behält DEFAULT + NOT NULL
        self.assertEqual(self.ddl(mysql.dialect(), User.__table__.c.language),
                         ["ALTER TABLE users ADD COLUMN language VARCHAR(5) DEFAULT '' NOT NULL"])

    def test_postgres_and_sqlite_keep_text_default(self):
        from sqlalchemy.dialects import postgresql, sqlite
        for d in (postgresql.dialect(), sqlite.dialect()):
            self.assertEqual(self.ddl(d, User.__table__.c.totp_secret),
                             ["ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''"])
        self.assertEqual(self.ddl(postgresql.dialect(), User.__table__.c.totp_enabled),
                         ["ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT false NOT NULL"])

    def test_all_model_columns_compile(self):
        from sqlalchemy.dialects import mysql, postgresql, sqlite
        for d in (mysql.dialect(), postgresql.dialect(), sqlite.dialect()):
            for table in db.metadata.sorted_tables:
                for col in table.columns:
                    stmts = self.ddl(d, col)
                    self.assertTrue(stmts[0].startswith("ALTER TABLE "))
                    if d.name == "mysql" and any(t in stmts[0] for t in ("TEXT", "BLOB")):
                        self.assertNotIn("DEFAULT", stmts[0])


# ------------------------------------------------------------------ 8. Zeitzone
class TimezoneFallbackTest(TmpDir):
    def test_system_zoneinfo_present(self):
        zdir = Path(self.tmp, "sys")
        (zdir / "Europe").mkdir(parents=True)
        (zdir / "Europe" / "Berlin").write_bytes(b"TZif")
        env, calls = {"TZ": "Europe/Berlin"}, []
        self.assertEqual(_ensure_timezone(env, [str(zdir)], tzdata_root="", tzset=lambda: calls.append(1)),
                         "Europe/Berlin")
        self.assertEqual(env["TZ"], "Europe/Berlin")
        self.assertEqual(calls, [])

    def test_falls_back_to_tzdata_package(self):
        pkg = Path(self.tmp, "tzdata", "zoneinfo", "Europe")
        pkg.mkdir(parents=True)
        (pkg / "Berlin").write_bytes(b"TZif")
        env, calls = {"TZ": "Europe/Berlin"}, []
        res = _ensure_timezone(env, [str(Path(self.tmp, "leer"))], tzdata_root=str(pkg.parent),
                               tzset=lambda: calls.append(1))
        self.assertEqual(env["TZ"], ":" + str(pkg / "Berlin"))
        self.assertEqual(res, env["TZ"])
        self.assertEqual(calls, [1])

    def test_missing_everywhere_warns(self):
        env = {"TZ": "Europe/Berlin"}
        with self.assertLogs("catalytix", "WARNING") as cm:
            self.assertIsNone(_ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None))
        self.assertIn("Time zone TZ=Europe/Berlin not found – times in UTC", cm.output[0])
        self.assertEqual(env["TZ"], "Europe/Berlin")

    def test_posix_and_utc_need_no_file(self):
        for tz in ("CET-1CEST,M3.5.0,M10.5.0/3", "UTC", ""):
            env = {"TZ": tz}
            with mock.patch.object(catalytix.log, "warning") as w:
                _ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None)
            w.assert_not_called()

    def test_no_path_traversal(self):
        env = {"TZ": "../../etc/passwd"}
        with self.assertLogs("catalytix", "WARNING"):
            _ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None)
        self.assertEqual(env["TZ"], "../../etc/passwd")


# ------------------------------------------------------------------ 3./4./5. Skripte und Doku
class ScriptsAndDocsTest(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "update.sh")], check=True)
        subprocess.run(["sh", "-n", str(ROOT / "docker-entrypoint.sh")], check=True)

    def test_update_script_never_deletes_before_copy_and_backs_up_via_entrypoint(self):
        s = (ROOT / "update.sh").read_text()
        self.assertNotIn("rm -rf ./catalytix", s)
        self.assertIn('exec -T "$SERVICE" docker-entrypoint.sh flask --app wsgi backup', s)
        self.assertLess(s.index("2. Backup"), s.index("3. Swap the files"))
        self.assertNotIn("-cf - . |", s)

    def test_docs_use_entrypoint_for_exec(self):
        for p in ["README.md", "README.en.md", "catalytix/templates/help/de/maintenance.html",
                  "catalytix/templates/help/en/maintenance.html"]:
            text = (ROOT / p).read_text()
            self.assertNotIn("compose exec catalytix flask", text, p)
            self.assertNotIn("compose exec -T catalytix flask", text, p)
        for p in ["catalytix/templates/help/de/maintenance.html", "catalytix/templates/help/en/maintenance.html"]:
            self.assertIn("docker compose exec catalytix docker-entrypoint.sh", (ROOT / p).read_text())

    def test_docs_explain_secret_key(self):
        for p, word in (("README.md", "2FA"), ("README.en.md", "2FA"), (".env.example", "2FA")):
            text = (ROOT / p).read_text()
            self.assertIn("CATALYTIX_ACCEPT_NEW_KEY", text, p)
            self.assertIn(word, text)

    def test_entrypoint_handles_backup_dir(self):
        s = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("CATALYTIX_BACKUP_DIR", s)


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class UpdateScriptRunTest(TmpDir):
    """update.sh mit Fake-docker: normales Update und Fehler mitten im Austausch."""

    def setUp(self):
        super().setUp()
        t = Path(self.tmp)
        self.bin = t / "bin"
        self.bin.mkdir()
        self.log = t / "docker.log"
        (self.bin / "docker").write_text(
            '#!/bin/bash\necho "docker $*" >> "$DOCKER_LOG"\n'
            'args="$*"; args="${args/compose -p catalytix-prod /compose }"\n'
            'case "$args" in\n  "compose ps -q --status running catalytix") echo abc;;\n'
            '  "compose ps -q catalytix") echo abc;;\n'
            '  *com.docker.compose.project*) echo catalytix-prod;;\n'
            '  "inspect -f {{.State.Status}} abc") echo running;;\n'
            '  inspect*) echo healthy;;\nesac\nexit 0\n')
        (self.bin / "docker").chmod(0o755)
        # alte Installation
        self.inst = t / "inst mit leer"
        self._tree(self.inst, "1.0.0", extra="ALT")
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        (self.inst / "data").mkdir()
        (self.inst / "data" / "catalytix.db").write_text("db")
        (self.inst / ".env").write_text("X=1\n")
        os.chmod(self.inst, 0o750)
        # Paket 2.0.0 mit Oberordner
        pkg = t / "pkg" / "catalytix"
        self._tree(pkg, "2.0.0", extra="NEU")
        shutil.copy(ROOT / "update.sh", pkg / "update.sh")
        self.zip = t / "pakete" / "catalytix-2.0.0.zip"
        self.zip.parent.mkdir()
        with zipfile.ZipFile(self.zip, "w") as z:
            for f in pkg.rglob("*"):
                z.write(f, f.relative_to(pkg.parent))

    @staticmethod
    def _tree(d, version, extra):
        (d / "catalytix").mkdir(parents=True)
        (d / "catalytix" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (d / "catalytix" / extra).write_text(extra)
        for f in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (d / f).write_text("x\n")

    def run_update(self, extra_path=None):
        env = dict(os.environ, PATH=f"{extra_path + ':' if extra_path else ''}{self.bin}:{os.environ['PATH']}",
                   DOCKER_LOG=str(self.log))
        # relative Paketangabe aus einem anderen Ordner, Aufruf mit „sh“
        return subprocess.run(["sh", str(self.inst / "update.sh"), "-y", "../pakete/catalytix-2.0.0.zip"],
                              cwd=self.zip.parent.parent / "pkg", env=env, capture_output=True, text=True)

    def version(self):
        return (self.inst / "catalytix" / "__init__.py").read_text()

    def test_normal_update(self):
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2.0.0", self.version())
        self.assertFalse((self.inst / "catalytix" / "ALT").exists())
        self.assertTrue((self.inst / "catalytix" / "NEU").exists())
        self.assertEqual((self.inst / "data" / "catalytix.db").read_text(), "db")
        self.assertTrue((self.inst / ".env").exists())
        self.assertEqual(os.stat(self.inst).st_mode & 0o777, 0o750)
        self.assertEqual(list(self.inst.glob(".catalytix-update.*")), [])
        log = self.log.read_text()
        # Projektname des laufenden Containers wird übernommen (verlinkte Ordner, anderer Ordnername)
        self.assertIn("compose -p catalytix-prod exec -T catalytix docker-entrypoint.sh flask --app wsgi backup", log)
        self.assertIn("compose -p catalytix-prod up -d", log)

    def test_failure_mid_swap_keeps_old_install(self):
        stub = Path(self.tmp, "badmv")
        stub.mkdir()
        (stub / "mv").write_text(
            '#!/bin/bash\nn=$(( $(cat "$MVCOUNT" 2>/dev/null || echo 0) + 1 )); echo $n > "$MVCOUNT"\n'
            'if [ "$n" = 4 ]; then echo "mv: simulierter Fehler" >&2; exit 1; fi\nexec /bin/mv "$@"\n')
        (stub / "mv").chmod(0o755)
        before = sorted(str(p.relative_to(self.inst)) for p in self.inst.rglob("*"))
        with mock.patch.dict(os.environ, {"MVCOUNT": str(Path(self.tmp, "mvcount"))}):
            r = self.run_update(str(stub))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restored", r.stdout + r.stderr)
        self.assertIn("1.0.0", self.version())
        self.assertEqual(sorted(str(p.relative_to(self.inst)) for p in self.inst.rglob("*")), before)


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("setpriv")
                     and os.path.isdir("/dev/shm"), "braucht root, setpriv und /dev/shm")
class EntrypointBackupDirTest(unittest.TestCase):
    def test_backup_dir_chowned_and_privileges_dropped(self):
        w = tempfile.mkdtemp(dir="/dev/shm", prefix="cx-ep-")
        try:
            os.chmod(w, 0o755)
            os.makedirs(f"{w}/data")
            os.makedirs(f"{w}/bk/alt")
            Path(f"{w}/bk/alt/x.db").write_text("x")
            env = dict(os.environ, CATALYTIX_DATA_DIR=f"{w}/data", CATALYTIX_BACKUP_DIR=f"{w}/bk",
                       PUID="1000", PGID="1000")
            r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "sh", "-c",
                                'id -u; touch "$CATALYTIX_BACKUP_DIR/neu" "$CATALYTIX_DATA_DIR/b.db"'],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().splitlines()[-1], "1000")
            for p in ("bk", "bk/alt/x.db", "bk/neu", "data/b.db"):
                self.assertEqual(os.stat(f"{w}/{p}").st_uid, 1000, p)
            r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "true"], env=env,
                               capture_output=True, text=True)
            self.assertNotIn("setting owner", r.stdout)
        finally:
            shutil.rmtree(w, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
