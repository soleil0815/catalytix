"""3.0.3: build chain – exact tag and source commit in the image, release for the image's commit, cool-down also for
new dependencies, :latest never moved down, one concurrency group per publishing workflow."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import lock  # noqa: E402
import security_update  # noqa: E402
from test_update_sh import IMG, Base as UpdateBase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class RollbackTagTests(UpdateBase):
    def setUp(self):
        super().setUp()
        self.installed("3.0.0")
        self.publish("3.1.0")
        self.state["health"]["3.1.0"] = "unhealthy"

    def test_tag_label_wins(self):
        # Before: only "latest"/"previous" as local tags → pinned to the plain 3.0.0 without the security fixes
        self.state["images"][f"{IMG}:latest"]["tag"] = "3.0.0-s3"
        self.run_update("-y")
        self.assertIn("CATALYTIX_TAG=3.0.0-s3", self.env())
        self.assertNotIn("may lack those fixes", self.out)

    def test_unknown_tag_is_pinned_with_a_warning(self):
        self.run_update("-y")
        self.assertIn("CATALYTIX_TAG=3.0.0", self.env())
        self.assertIn("may lack those fixes", self.out)

    def test_label_of_another_version_is_ignored(self):
        self.state["images"][f"{IMG}:latest"]["tag"] = "9.9.9-s1"
        self.run_update("-y")
        self.assertIn("CATALYTIX_TAG=3.0.0\n", self.env())


class CooldownTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 9, 4, 17, tzinfo=dt.timezone.utc)

    def release(self, uploaded):
        return {"urls": [{"digests": {"sha256": "a" * 64}, "upload_time_iso_8601": uploaded}]}

    def plan(self, old, resolved, uploaded):
        cutoff = self.NOW - dt.timedelta(days=7)
        with mock.patch.object(lock, "pypi_release", return_value=self.release(uploaded)), \
                mock.patch.object(lock, "newest_on_pypi", side_effect=lambda n, v, c: v):
            return lock.plan_python(old, resolved, cutoff, "26.2.2")

    def test_new_dependency_younger_than_cooldown_refused(self):
        old = {"flask": {"name": "Flask", "version": "3.1.3", "marker": "", "hashes": ["a" * 64]}}
        with self.assertRaises(SystemExit) as cm:
            self.plan(old, {"flask": "3.1.3", "evil": "1.0"}, "2026-11-08T10:00:00Z")
        self.assertIn("evil 1.0", str(cm.exception))
        new, changes, _ = self.plan(old, {"flask": "3.1.3", "fine": "1.0"}, "2026-10-01T10:00:00Z")
        self.assertIn("pip fine: new 1.0", changes)

    def test_fresh_lower_release_refused(self):
        old = {"flask": {"name": "Flask", "version": "3.1.3", "marker": "", "hashes": ["a" * 64]}}
        with self.assertRaises(SystemExit):
            self.plan(old, {"flask": "3.1.4"}, "2026-11-08T10:00:00Z")

    def test_unchanged_version_needs_no_check(self):
        old = {"flask": {"name": "Flask", "version": "3.1.3", "marker": "", "hashes": ["a" * 64]}}
        cutoff = self.NOW - dt.timedelta(days=7)
        with mock.patch.object(lock, "pypi_release", side_effect=AssertionError("no lookup")), \
                mock.patch.object(lock, "newest_on_pypi", side_effect=lambda n, v, c: v):
            new, changes, _ = lock.plan_python(old, {"flask": "3.1.3"}, cutoff, "26.2.2")
        self.assertEqual(changes, [])

    def test_cooldown_zero_takes_everything(self):
        old = {"flask": {"name": "Flask", "version": "3.1.3", "marker": "", "hashes": ["a" * 64]}}
        with mock.patch.object(lock, "pypi_release", return_value=self.release("2026-11-09T04:00:00Z")), \
                mock.patch.object(lock, "newest_on_pypi", side_effect=lambda n, v, c: v):
            new, changes, _ = lock.plan_python(old, {"flask": "3.1.4"}, None, "26.2.2")
        self.assertIn("pip Flask: 3.1.3 -> 3.1.4", changes)


class LabelTests(unittest.TestCase):
    def test_label_from_multi_platform_index(self):
        data = {"linux/amd64": {"config": {"Labels": {"org.opencontainers.image.version": "3.0.3"}}},
                "linux/arm64": {"config": {"Labels": {"org.opencontainers.image.version": "3.0.3"}}}}
        self.assertEqual(security_update.image_label(json.dumps(data), "org.opencontainers.image.version"), "3.0.3")
        single = {"config": {"Labels": {"catalytix.tag": "3.0.3-s1"}}}
        self.assertEqual(security_update.image_label(json.dumps(single), "catalytix.tag"), "3.0.3-s1")
        self.assertEqual(security_update.image_label(json.dumps(single), "missing"), "")
        self.assertIsNone(security_update.image_label("not json", "x"))

    def run_ci(self, script, inspect_out, inspect_rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "docker"
            # imagetools inspect REF            → exists;  --format '{{json .Image}}' → the labels
            f.write_text("#!/bin/sh\ncase \"$*\" in *json*) cat \"$INSPECT\" ;; *) echo ok ;; esac\n"
                         f"exit {inspect_rc}\n")
            f.chmod(0o755)
            out = Path(tmp) / "inspect.json"
            out.write_text(inspect_out)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GITHUB_REPOSITORY="o/r", INSPECT=str(out))
            return subprocess.run(["bash", "-c", f"set -e; . tools/ci.sh; {script}"], cwd=ROOT, env=env,
                                  capture_output=True, text=True)

    def test_latest_never_moved_down(self):
        labels = json.dumps({"config": {"Labels": {"org.opencontainers.image.version": "3.1.0"}}})
        self.assertNotEqual(self.run_ci("latest_allows 3.0.3", labels).returncode, 0)
        self.assertEqual(self.run_ci("latest_allows 3.1.0", labels).returncode, 0)     # security update of 3.1.0
        self.assertEqual(self.run_ci("latest_allows 3.2.0", labels).returncode, 0)


class WorkflowTests(unittest.TestCase):
    def test_tag_and_revision_baked_in(self):
        docker = (ROOT / "Dockerfile").read_text()
        self.assertIn('catalytix.tag="${CATALYTIX_TAG}"', docker)
        self.assertIn('org.opencontainers.image.revision="${CATALYTIX_REVISION}"', docker)
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text()
        self.assertEqual(build.count('CATALYTIX_TAG=$VERSION'), 2)       # the build and the push
        upd = (ROOT / ".github" / "workflows" / "security-update.yml").read_text()
        self.assertIn('--build-arg "CATALYTIX_TAG=$TAG"', upd)
        self.assertLess(upd.index("next-tag"), upd.index("--no-cache"))   # chosen before the build

    def test_release_points_to_the_image_commit(self):
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text()
        self.assertIn('--target "$TARGET"', build)
        self.assertNotIn('--target "$GITHUB_SHA"', build)
        self.assertIn("org.opencontainers.image.revision", build)

    def test_latest_guarded_in_both_workflows(self):
        for name in ("build.yml", "security-update.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("latest_allows", text, name)


if __name__ == "__main__":
    unittest.main()
