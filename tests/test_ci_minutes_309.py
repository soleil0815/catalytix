"""3.0.9: fewer GitHub Actions minutes (private repository: limited free minutes) – without weaker checks."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from catalytix import create_app  # noqa: E402
from catalytix.models import User, db, hash_secret  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def workflow(name):
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def job(text, name):
    m = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|\Z)", text.split("\njobs:\n", 1)[1])
    return m.group(1)


class PasswordHashTests(unittest.TestCase):
    def test_operation_keeps_scrypt(self):
        app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "k", "TESTING": False})
        with app.app_context():
            self.assertTrue(hash_secret("x").startswith("scrypt:"))
            u = User(username="a")
            u.set_password("geheim123")
            self.assertTrue(u.password_hash.startswith("scrypt:"))
            self.assertTrue(u.check_password("geheim123"))
            db.session.remove()

    def test_self_tests_use_cheap_hash_that_still_checks(self):
        app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite://", "SECRET_KEY": "k", "TESTING": True})
        with app.app_context():
            u = User(username="a")
            u.set_password("geheim123")
            self.assertTrue(u.password_hash.startswith("pbkdf2:"))
            self.assertTrue(u.check_password("geheim123"))
            self.assertFalse(u.check_password("falsch"))
            db.session.remove()

    def test_without_app_context_scrypt(self):
        self.assertTrue(hash_secret("x").startswith("scrypt:"))


class WorkflowTests(unittest.TestCase):
    def test_no_run_on_push(self):
        on = workflow("build.yml").split("\npermissions:")[0]
        self.assertNotIn("push:", on)
        self.assertNotIn("paths-ignore", on)      # docs and LICENSE go into the image – every PR is checked

    def test_publish_run_does_not_build_twice(self):
        check = job(workflow("build.yml"), "check")
        for step in ("Prepare Docker", "Build the image", "Run the tests in the image", "Start the image",
                     "Security scan"):
            block = check[check.index(step):]
            self.assertIn("if: steps.version.outputs.new != 'true'", block[:200], step)
        mypy = check[check.index("Type check (mypy)"):]
        self.assertNotIn("if:", mypy[:mypy.index("- name:")])                          # mypy always runs
        platform = job(workflow("build.yml"), "platform")
        for part in ("--no-cache", "test_in_image", "start_in_image", "trivy_scan.py scan"):
            self.assertIn(part, platform)
        for arch, runner in (("amd64", "ubuntu-24.04"), ("arm64", "ubuntu-24.04-arm")):
            self.assertIn(f"- arch: {arch}\n            runner: {runner}\n", platform)

    def test_security_update_builds_arm64_only_when_something_changed(self):
        upd = job(workflow("security-update.yml"), "update")
        amd = upd[upd.index("Build amd64 (without cache)"):upd.index("Compare with the published")]
        self.assertNotIn("arm64", amd.split("run:", 1)[1])
        self.assertNotIn("linux/arm64", upd)                             # 3.1.5: own job, natively
        arm = job(workflow("security-update.yml"), "arm64")
        self.assertIn("if: needs.update.outputs.changed == 'true'", arm)

    def test_pull_request_checks_arm64_natively(self):
        arm = job(workflow("build.yml"), "check-arm64")
        self.assertIn("if: github.event_name == 'pull_request'", arm)
        self.assertIn("runs-on: ubuntu-24.04-arm", arm)
        for part in ("--no-cache", "--platform linux/arm64", "test_in_image", "start_in_image", "trivy_scan.py scan"):
            self.assertIn(part, arm)

    def test_no_emulation(self):
        # 3.1.5: arm64 on a native ARM runner – under QEMU the publish run took 20+ minutes
        self.assertNotIn("qemu-user-static", (ROOT / "tools" / "ci.sh").read_text())
        for name in ("build.yml", "security-update.yml"):
            self.assertNotIn("setup-qemu", workflow(name), name)
            self.assertNotIn("prepare_builder multi", workflow(name), name)
            self.assertNotIn("linux/amd64,linux/arm64", workflow(name), name)


if __name__ == "__main__":
    unittest.main()
