"""3.0.1: Typprüfung (mypy) – Fehler, die sie gefunden hat, und die Prüfung selbst."""
import configparser
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app, i18n, ldap_auth  # noqa: E402
from catalytix.models import db  # noqa: E402


class FakeConn:
    """ldap3.Connection, dessen Bind vom Server abgelehnt wird."""

    def __init__(self, *a, **kw):
        self.result = {"description": "invalidCredentials", "message": "80090308: LdapErr: DSID-0C09042A"}
        self.unbound = False

    def open(self, **kw):
        return True

    def start_tls(self, **kw):
        return True

    def bind(self, **kw):
        return False

    def unbind(self):
        self.unbound = True


class BindRejectedTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_rejected_bind_gives_message_not_typeerror(self):
        # Vorher: _("…{msg}", msg=…) → TypeError „got multiple values for argument 'msg'“, die Anmeldung brach ab.
        d = ldap_auth.Directory({"ldap_server": "ldap://dc.firma.local", "ldap_base_dn": "DC=firma,DC=local"})
        with mock.patch("ldap3.Connection", FakeConn), mock.patch.object(ldap_auth.Directory, "_server_pool",
                                                                         return_value=object()):
            with self.assertRaises(ldap_auth.LDAPError) as cm:
                d.connect("CN=svc,DC=firma,DC=local", "geheim")
        self.assertIn("invalidCredentials", str(cm.exception))
        self.assertIn("80090308", str(cm.exception))

    def test_placeholders_named_like_parameters(self):
        for name in ("msg", "ctx", "singular", "plural", "n"):
            self.assertEqual(i18n.gettext("{%s}!" % name, **{name: "x"}), "x!")
        self.assertEqual(i18n.pgettext("k", "{msg}/{ctx}", msg="a", ctx="b"), "a/b")
        self.assertEqual(i18n.ngettext("{msg} {n}", "{msg} {n}s", 2, msg="Buch"), "Buch 2s")
        self.assertEqual(str(i18n.lazy_gettext("{msg}", msg="y")), "y")
        self.assertEqual(str(i18n.template_gettext("{msg}", msg="<b>")), "&lt;b&gt;")


class MypySetupTests(unittest.TestCase):
    STRICT = ["catalytix.backup", "catalytix.netzone", "catalytix.proxy", "catalytix.models", "tools.*"]

    def setUp(self):
        self.cfg = configparser.ConfigParser()
        self.cfg.read(ROOT / "mypy.ini")

    def test_strict_modules_listed(self):
        sections = [s for s in self.cfg.sections() if s.startswith("mypy-")]
        strict = {m.strip() for s in sections for m in s[5:].split(",")
                  if self.cfg[s].get("disallow_untyped_defs") == "True"}
        for mod in self.STRICT:
            self.assertIn(mod, strict)

    def test_lenient_default_and_strict_flags(self):
        self.assertEqual(self.cfg["mypy"].get("ignore_errors"), "True")
        sec = next(s for s in self.cfg.sections() if "catalytix.backup" in s)
        for flag in ("ignore_errors = False", "disallow_untyped_defs", "disallow_incomplete_defs",
                     "warn_return_any", "strict_optional", "check_untyped_defs"):
            key = flag.split(" = ")[0]
            self.assertIn(key, self.cfg[sec], flag)

    def test_mypy_pinned_with_hashes(self):
        lock = (ROOT / "tools" / "mypy.lock").read_text()
        self.assertRegex(lock, r"(?m)^mypy==\d+\.\d+\.\d+ \\$")
        for line in lock.splitlines():
            if re.match(r"^[A-Za-z0-9_.-]+==", line):
                self.assertTrue(line.endswith("\\"), line)
        self.assertIn("--hash=sha256:", lock)

    def test_ci_runs_mypy_strict_and_report(self):
        ci = (ROOT / "tools" / "ci.sh").read_text()
        self.assertIn("mypy", ci)
        self.assertIn("--require-hashes", ci)
        self.assertIn("mypy-report.ini", ci)
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text()
        self.assertIn("mypy", build)


if __name__ == "__main__":
    unittest.main()
