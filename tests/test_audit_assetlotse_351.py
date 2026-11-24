"""3.5.1: findings of the AssetLotse code audit 2026-11 that apply to Catalytix as well (same code base):
personal values in the log are marked and replaced as a whole in the diagnostic package; emergency commands ignore
upper/lower case like the sign-in; the admin user page counts passkeys separately; a broken settings anchor does not
stop the page script. (HTTPS per proxy address: tests/test_proxy.py; passkeys of AD accounts only with the AD
identifier: tests/test_passkeys_330.py.)"""
import ast
import io
import logging
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import diagnostics  # noqa: E402
from catalytix.logmark import MARK_END, MARK_START, personal  # noqa: E402
from catalytix.models import DiagAlias, User, WebAuthnCredential, db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("catalytix.views.main")


def unpack(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {Path(n).name: z.read(n).decode("utf-8") for n in z.namelist()}


class MarkedLogTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.folder = Path(self.app.instance_path)
        diagnostics.install_log_file(self.folder)
        self.make_user("admin", "admin")
        self.login("admin")

    def tearDown(self):
        diagnostics.uninstall_log_file(self.folder)
        for f in (self.folder / "logs").glob("catalytix-*.log"):
            f.unlink()
        super().tearDown()

    def package(self):
        r = self.c.post("/admin/diagnosepaket", data={"tage": "1", "ohne_tests": "1"})
        self.assertEqual(r.status_code, 200)
        return unpack(r.data)

    def test_password_typed_as_user_name_is_not_in_the_package_nor_stored(self):
        log.warning("LDAP sign-in %s failed: %s", personal("Sommer!Kennwort77"), "invalid credentials")
        files = self.package()
        everything = "\n".join(files.values())
        self.assertNotIn("Sommer!Kennwort77", everything)
        self.assertNotIn("sommer!kennwort77", everything.lower())
        self.assertIn("LDAP sign-in UNBEKANNT-01 failed", files["log.txt"])
        self.assertIsNone(DiagAlias.query.filter(DiagAlias.real.like("%sommer%")).first())   # never stored
        self.assertNotIn(MARK_START, everything)
        self.assertNotIn(MARK_END, everything)

    def test_deleted_account_is_not_in_the_package(self):
        u = self.make_user("kurzzeitgast")
        r = self.c.post(f"/admin/benutzer/{u.id}/loeschen", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(User.query.filter_by(username="kurzzeitgast").first())
        files = self.package()
        self.assertNotIn("kurzzeitgast", "\n".join(files.values()).lower())

    def test_known_account_keeps_its_permanent_placeholder(self):
        self.make_user("lesekind")
        log.info("Sign-in of %s", personal("lesekind"))
        files = self.package()
        alias = DiagAlias.query.filter_by(kind="user", real="lesekind").first()
        self.assertIsNotNone(alias)
        self.assertIn(f"Sign-in of {alias.alias}", files["log.txt"])

    def test_user_name_that_is_a_program_word(self):
        self.make_user("backup")
        log.info("Sign-in of %s", personal("backup"))
        line = [ln for ln in self.package()["log.txt"].splitlines() if "Sign-in of" in ln][0]
        self.assertNotIn("backup", line.lower())

    def test_line_breaks_in_a_marked_value_stay_one_piece(self):
        log.info("Desk search %s", personal("Hildegard\nQuaschning", "text"))
        text = self.package()["log.txt"]
        self.assertNotIn("Quaschning", text)
        self.assertNotIn("Hildegard", text)
        self.assertIn("Desk search EINGABE-01", text)
        self.assertNotIn("t:", text)

    def test_other_log_outputs_unchanged(self):
        record = logging.LogRecord("catalytix.x", logging.INFO, __file__, 1, "Sign-in of %s", (personal("anna"),), None)
        self.assertEqual(logging.Formatter("%(message)s").format(record), "Sign-in of anna")

    def test_every_logged_user_name_is_marked(self):
        # guard for future log lines: usernames in log calls go through logmark.personal()
        missing = []
        for path in (ROOT / "catalytix").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
                    marked = False
                    for arg in node.args[1:]:
                        text = ast.unparse(arg)
                        marked = marked or text.startswith("personal(")
                        if text == "username" or text.endswith(".username") or text in ("display_name", "query"):
                            missing.append(f"{path.name}:{node.lineno} {text}")
                    # the marks only survive %s (with %r they would be escaped and the value stay in clear)
                    fmt = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else ""
                    if marked and "%r" in str(fmt):
                        missing.append(f"{path.name}:{node.lineno} %r")
        self.assertEqual(missing, [])


class EmergencyCommandCaseTests(BaseCase):
    def test_commands_ignore_case_like_the_sign_in(self):
        self.make_user("Admin", "admin")
        runner = self.app.test_cli_runner()
        r = runner.invoke(args=["reset-2fa", "admin"])
        self.assertEqual(r.exit_code, 0, r.output)
        r = runner.invoke(args=["reset-password", "ADMIN"], input="neuesKennwort123\nneuesKennwort123\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertTrue(User.query.filter_by(username="Admin").first().check_password("neuesKennwort123"))
        r = runner.invoke(args=["create-admin", "admin"], input="nochEinKennwort1\nnochEinKennwort1\n")
        self.assertNotEqual(r.exit_code, 0)                 # no second "admin" next to "Admin"
        self.assertEqual(User.query.count(), 1)


class AdminUserPageTests(BaseCase):
    def test_passkeys_counted_separately(self):
        self.make_user("admin", "admin")
        u = self.make_user("leser")
        for n, pk in ((1, False), (2, True), (3, True)):
            db.session.add(WebAuthnCredential(user_id=u.id, credential_id=f"c{n}", credential_data="x",
                                              rp_id="localhost", passkey=pk))
        db.session.commit()
        self.login("admin")
        page = self.c.get(f"/admin/benutzer/{u.id}").get_data(as_text=True)
        self.assertIn("1 Sicherheitsschlüssel + 2 Passkeys", page)
        self.assertIn("App, Sicherheitsschlüssel und Passkeys werden entfernt", page)


class SettingsAnchorTests(unittest.TestCase):
    def test_broken_anchor_cannot_stop_the_script(self):
        js = (ROOT / "catalytix" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        for m in re.finditer(r"decodeURIComponent\(", js):
            line = js[js.rfind("\n", 0, m.start()) + 1:js.find("\n", m.start())]
            self.assertIn("try", line, line)


if __name__ == "__main__":
    unittest.main()
