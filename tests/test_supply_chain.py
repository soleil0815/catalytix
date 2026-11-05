"""Self-tests for build and delivery: pinned versions, limits of the foundation, workflows, Trivy flow.

Without PyYAML (not a dependency of the program) – the workflows are checked as text.
"""
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "tools"))

import foundation_check  # noqa: E402
import lock  # noqa: E402
import security_update  # noqa: E402
import trivy_scan  # noqa: E402


def workflow(name: str) -> str:
    return (WF / name).read_text(encoding="utf-8")


def jobs(text: str) -> dict[str, str]:
    """Job name -> its text block (top-level keys under 'jobs:')."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"\n  (?=[a-z][\w-]*:\n)", "\n" + body)
    return {p.split(":", 1)[0].strip(): p for p in parts if p.strip()}


def step_names(block: str) -> list[str]:
    return re.findall(r"^\s+- (?:name: (.+)|uses: (\S+))$", block, re.M)


class VersionLimitTests(unittest.TestCase):
    def test_major_version_limit(self):
        self.assertTrue(lock.within("3.9.0", "3.1.3"))
        self.assertFalse(lock.within("4.0.0", "3.1.3"))
        self.assertFalse(lock.within("3.1.2", "3.1.3"))          # never back
        self.assertEqual(lock.bound_spec("Flask", "3.1.3"), "Flask>=3.1.3,<4")

    def test_before_1_0_minor_limit(self):
        self.assertTrue(lock.within("0.6.9", "0.6.3"))
        self.assertFalse(lock.within("0.7.0", "0.6.3"))
        self.assertEqual(lock.upper_bound("0.6.3"), (0, 7))

    def test_calendar_versions_unlimited(self):
        for v in ("2026.10.19", "2025.2"):
            self.assertTrue(lock.is_calendar_version(v))
            self.assertIsNone(lock.upper_bound(v))
        self.assertTrue(lock.within("2027.1.5", "2026.10.19"))

    def test_only_final_releases(self):
        for v in ("3.2.0rc1", "3.2.0b2", "3.2.0.dev1", "3.2.0a1"):
            self.assertTrue(lock.is_prerelease(v), v)
        for v in ("3.2.0", "1.0.post1", "26.2"):
            self.assertFalse(lock.is_prerelease(v), v)

    def test_cooldown_never_excludes_the_locked_version(self):
        now = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
        pypi = {"releases": {
            "2026.10.19": [{"upload_time_iso_8601": "2026-10-19T10:00:00Z"}],   # 6 days – too young
            "2026.7.22": [{"upload_time_iso_8601": "2026-07-22T10:00:00Z"}]}}
        with mock.patch.object(lock, "pypi_project", return_value=pypi):
            self.assertEqual(lock.newest_on_pypi("certifi", "2026.10.19", now - dt.timedelta(days=7)), "2026.10.19")
            self.assertEqual(lock.newest_on_pypi("certifi", "2026.7.22", now - dt.timedelta(days=7)), "2026.7.22")
            self.assertEqual(lock.newest_on_pypi("certifi", "2026.7.22", None), "2026.10.19")

    def test_newest_stays_inside_the_major_version(self):
        pypi = {"releases": {v: [{"upload_time_iso_8601": "2026-01-01T00:00:00Z"}]
                             for v in ("3.1.3", "3.1.9", "3.4.0", "4.0.0", "4.1.0rc1")}}
        with mock.patch.object(lock, "pypi_project", return_value=pypi):
            self.assertEqual(lock.newest_on_pypi("Flask", "3.1.3", None), "3.4.0")

    def test_constraints_cover_packages_with_markers(self):
        old = {"gunicorn": {"name": "gunicorn", "version": "26.2.0", "marker": 'sys_platform != "win32"', "hashes": []},
               "certifi": {"name": "certifi", "version": "2026.10.19", "marker": "", "hashes": []}}
        text = lock.constraints_for(old, None, newest=lambda n, v, c: v)
        self.assertIn("gunicorn>=26.2.0,<=26.2.0", text)        # gunicorn gets the cool-down too
        self.assertIn("certifi>=2026.10.19,<=2026.10.19", text)

    def test_pip_has_no_upper_limit(self):
        self.assertIsNone(lock.upper_bound("26.2.2", "pip"))
        self.assertEqual(lock.upper_bound("26.2.0", "gunicorn"), (27,))

    def test_snapshot_is_a_complete_past_state(self):
        with mock.patch.object(lock, "newest_base_image", side_effect=SystemExit("stop")):
            with self.assertRaises(SystemExit):
                lock.update(now=dt.datetime(2026, 10, 26, 4, 17, tzinfo=dt.timezone.utc))
        src = (ROOT / "tools" / "lock.py").read_text()
        self.assertIn('(now - dt.timedelta(days=1)).strftime("%Y%m%dT000000Z")', src)

    def test_resolver_result_outside_foundation_refused(self):
        old = {"flask": {"name": "Flask", "version": "3.1.3", "marker": "", "hashes": ["a" * 64]}}
        with self.assertRaises(SystemExit):
            lock.plan_python(old, {"flask": "4.0.0"}, None, "26.2.2")

    def test_base_image_outside_foundation_refused(self):
        def manifest(repo, ref, token):
            if ref.startswith("sha256:aa"):
                return {"config": {"digest": "sha256:cfg"}}, ""
            return {"manifests": [{"digest": "sha256:" + "a" * 64,
                                   "platform": {"os": "linux", "architecture": "amd64"}}]}, "sha256:" + "b" * 64
        with mock.patch.object(lock, "registry_token", return_value="t"), \
                mock.patch.object(lock, "registry_manifest", side_effect=manifest), \
                mock.patch.object(lock, "registry_blob", return_value={"config": {"Env": ["PYTHON_VERSION=3.14.0"]}}):
            with self.assertRaises(SystemExit):
                lock.newest_base_image()
        with mock.patch.object(lock, "registry_token", return_value="t"), \
                mock.patch.object(lock, "registry_manifest", side_effect=manifest), \
                mock.patch.object(lock, "registry_blob", return_value={"config": {"Env": ["PYTHON_VERSION=3.13.18"]}}):
            image, version, debian = lock.newest_base_image()
        self.assertEqual(image, "python:3.13.18-slim-trixie@sha256:" + "b" * 64)
        self.assertEqual((version, debian), ("3.13.18", "trixie"))


class LockFileTests(unittest.TestCase):
    def test_dockerfile_locks_tag_digest_and_snapshot(self):
        d = lock.read_dockerfile()
        self.assertRegex(d["image"], r"^python:3\.13\.\d+-slim-trixie@sha256:[0-9a-f]{64}$")
        self.assertRegex(d["snapshot"], r"^\d{8}T\d{6}Z$")
        text = (ROOT / "Dockerfile").read_text()
        self.assertIn("snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}", text)
        self.assertIn('[ "$VERSION_CODENAME" = trixie ]', text)
        self.assertIn("full-upgrade", text)                       # also the base image's packages, nothing held back
        self.assertIn("Packages left out of the upgrade", text)
        self.assertIn("--require-hashes", text)
        self.assertIn("python -m pip uninstall -y pip", text)     # package manager not in the finished image
        # not "! cmd" under set -e (POSIX ignores -e for negated commands) – an explicit exit
        self.assertIn('if command -v pip >/dev/null || python -c "import pip" 2>/dev/null; then echo', text)

    def test_every_package_pinned_with_hash(self):
        problems = lock.check()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_requirements_in_ranges_follow_the_rule(self):
        for name, line in lock.requirements_in().items():
            m = re.search(r">=([\d.]+),<([\d.]+)", line)
            self.assertTrue(m, line)
            low, high = m.group(1), m.group(2)
            self.assertEqual(tuple(int(x) for x in high.split(".")), lock.upper_bound(low), line)

    def test_copy_locks_and_same_build(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for d in (a, b):
                Path(d, "docker").mkdir()
                Path(d, "requirements.in").write_text((ROOT / "requirements.in").read_text())
                Path(d, "requirements.txt").write_text("x==1\n")
                Path(d, "docker", "pip.lock").write_text("pip==1\n")
            df = (ROOT / "Dockerfile").read_text()
            Path(a, "Dockerfile").write_text(df)
            Path(b, "Dockerfile").write_text(lock.SNAPSHOT_RE.sub("ARG DEBIAN_SNAPSHOT=20990101T000000Z", df))
            self.assertEqual(lock.main(["same-build", a, b]), 0)        # only lock values differ
            lock.copy_locks(Path(b), Path(a))
            self.assertIn("ARG DEBIAN_SNAPSHOT=20990101T000000Z", Path(a, "Dockerfile").read_text())
            Path(b, "requirements.in").write_text("Flask>=3.1,<4\nneu>=1,<2\n")
            with mock.patch("sys.stdout"):     # do not output the error message as a real annotation on the GitHub run
                self.assertEqual(lock.main(["same-build", a, b]), 1)    # unpublished build change


class WorkflowTests(unittest.TestCase):
    def all_workflows(self):
        return {p.name: p.read_text() for p in WF.glob("*.yml")}

    def test_no_bootstrap_workflow_left(self):
        self.assertFalse((WF / "bootstrap-lock.yml").exists())

    def test_actions_pinned_to_commits_and_no_trivy_action(self):
        for name, text in self.all_workflows().items():
            for ref in re.findall(r"uses:\s*(\S+)", text):
                self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", f"{name}: {ref}")
            self.assertNotIn("trivy-action", text, name)
            self.assertNotIn("setup-trivy", text, name)

    def test_no_stored_git_credentials(self):
        for name, text in self.all_workflows().items():
            checkouts = text.count("uses: actions/checkout@")
            self.assertEqual(text.count("persist-credentials: false"), checkouts, name)

    def test_minimal_permissions(self):
        for name, text in self.all_workflows().items():
            self.assertIn("\npermissions: {}\n", text, f"{name}: no default permissions")
            for job, block in jobs(text).items():
                self.assertIn("permissions:", block, f"{name}/{job}")
                self.assertNotIn("write-all", block)
        self.assertNotIn("pull_request_target", "".join(self.all_workflows().values()))
        # write permissions only where publishing happens
        self.assertNotIn(": write", jobs(workflow("build.yml"))["check"])
        self.assertNotIn("contents: write", jobs(workflow("security-update.yml"))["update"])
        self.assertNotIn("packages: write", jobs(workflow("security-update.yml"))["record"])
        for name in ("scan.yml", "foundation.yml"):
            self.assertNotIn(": write", workflow(name), name)

    def test_schedules(self):
        self.assertRegex(workflow("security-update.yml"), r'cron: "\d+ [0-6] \* \* 1"')      # Monday morning
        self.assertRegex(workflow("scan.yml"), r'cron: "\d+ \d+ \* \* \*"')                  # daily
        self.assertRegex(workflow("foundation.yml"), r'cron: "\d+ \d+ \* \* 1"')             # weekly
        for name in ("security-update.yml", "scan.yml", "foundation.yml"):
            self.assertIn("workflow_dispatch:", workflow(name), name)                        # can be started manually

    def test_publishing_jobs_never_concurrent_and_only_from_main(self):
        publishing = [("build.yml", "publish"), ("build.yml", "release"), ("security-update.yml", "update"),
                      ("security-update.yml", "record"), ("security-update.yml", "promote")]
        for name, job in publishing:
            block = jobs(workflow(name))[job]
            # 3.0.3: one group per workflow – a group shared by both let GitHub cancel a waiting job when a third
            # one queued (e.g. the release job after the image was pushed)
            group = "publish-build" if name == "build.yml" else "publish-security"
            self.assertIn(f"group: {group}\n", block, f"{name}/{job}")
            self.assertIn("cancel-in-progress: false", block, f"{name}/{job}")
        self.assertIn("github.ref == 'refs/heads/main'", jobs(workflow("build.yml"))["publish"])
        self.assertIn("if: github.ref == 'refs/heads/main'", jobs(workflow("security-update.yml"))["update"])

    def test_release_order(self):
        wf = jobs(workflow("build.yml"))
        check = wf["check"]
        order = ["Check locks", "Build the image (without cache)", "Run the tests in the image", "Security scan"]
        positions = [check.index(o) for o in order]
        self.assertEqual(positions, sorted(positions))
        publish = wf["publish"]
        order = ["Already published?", "--no-cache", "test_in_image", "scan catalytix-ci:amd64",
                 "scan catalytix-ci:arm64", "--push", "imagetools create -t"]
        positions = [publish.index(o) for o in order]
        self.assertEqual(positions, sorted(positions), order)
        self.assertIn("needs: check", publish)
        self.assertIn("is-highest", check)
        self.assertIn("is-highest", publish)                 # :latest only for the highest version
        self.assertIn("needs: [check, publish]", wf["release"])

    def test_security_update_order(self):
        wf = jobs(workflow("security-update.yml"))
        upd = wf["update"]
        order = ["highest_release", "same-build", "lock.py update", "next-tag", "--no-cache",
                 "Compare with the published", "test_in_image", "scan catalytix-ci:amd64", "scan catalytix-ci:arm64",
                 "--push", "upload-artifact"]
        positions = [upd.index(o) for o in order]
        self.assertEqual(positions, sorted(positions), order)
        for step in ("Run the tests", "Security scan", "Push the security update"):
            block = upd[upd.index(step):]
            self.assertIn("if: steps.compare.outputs.changed == 'true'", block[:300], step)   # otherwise do nothing
        self.assertIn("while image_exists", upd)                                              # never overwrite
        rec = wf["record"]
        self.assertIn("if: needs.update.outputs.changed == 'true'", rec)
        self.assertLess(rec.index("commits/main"), rec.index("changelog"))                   # main not moved
        self.assertLess(rec.index("changelog"), rec.index("git push"))
        self.assertIn("needs: [update, record]", wf["promote"])
        self.assertIn("is-highest", wf["promote"])

    def test_errors_are_readable_on_the_run(self):
        ci = (ROOT / "tools" / "ci.sh").read_text()
        self.assertIn('echo "::error title=$1::$2"', ci)
        for name in ("build.yml", "security-update.yml"):
            self.assertGreaterEqual(workflow(name).count("fail \""), 6, name)


class PublishByHandTests(unittest.TestCase):
    """3.0.0: A merged pull request publishes nothing – only "Run workflow" with "publish"; no ZIP any more."""

    def test_publish_only_by_hand(self):
        wf = workflow("build.yml")
        j = jobs(wf)
        self.assertIn("publish:", wf.split("workflow_dispatch:")[1].split("permissions:")[0])
        self.assertIn('[ "$GITHUB_EVENT_NAME" = workflow_dispatch ] && [ "$PUBLISH" = true ]', j["check"])
        self.assertIn("github.event_name == 'workflow_dispatch'", j["publish"])
        self.assertIn("Not published yet", j["check"])

    def test_release_without_zip_with_image_files(self):
        rel = jobs(workflow("build.yml"))["release"]
        self.assertNotIn(".zip", rel)
        self.assertNotIn("git archive", rel)
        for part in ("for arch in amd64 arm64", 'docker save "$image:$VERSION"', "SHA256SUMS",
                     'docker cp "$cid:/app/dist/." dist/', 'cp "dist/$f" assets/', "env.example"):
            self.assertIn(part, rel)
        self.assertNotIn("cp update.sh", rel)              # 3.0.3: from the image, not from the checkout
        self.assertIn("packages: read", rel)


class CiHelperTests(unittest.TestCase):
    """tools/ci.sh with fake docker/gh: registry errors never count as "not published"."""

    def run_sh(self, script, docker_out="", docker_rc=0, gh_out="", gh_rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            for name, out, rc in (("docker", docker_out, docker_rc), ("gh", gh_out, gh_rc)):
                f = Path(tmp) / name
                f.write_text(f"#!/bin/sh\nprintf '%s\\n' '{out}'\nexit {rc}\n")
                f.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GITHUB_REPOSITORY="o/r")
            # Like GitHub without "shell: bash" (only -e, no pipefail): ci.sh must still detect errors
            return subprocess.run(["bash", "-c", f"set -e; . tools/ci.sh; {script}"], cwd=ROOT,
                                  env=env, capture_output=True, text=True)

    def test_image_exists(self):
        self.assertEqual(self.run_sh("image_exists x:1").returncode, 0)
        r = self.run_sh("image_exists x:1 || echo missing", "ERROR: x:1: not found", 1)
        self.assertIn("missing", r.stdout)
        r = self.run_sh("image_exists x:1 || echo missing", "connection reset by peer", 1)
        self.assertNotIn("missing", r.stdout)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("::error title=Registry not reachable", r.stdout)

    def test_release_exists(self):
        r = self.run_sh("release_exists 1.0.0 || echo missing", gh_out="gh: Not Found (HTTP 404)", gh_rc=1)
        self.assertIn("missing", r.stdout)
        r = self.run_sh("release_exists 1.0.0 || echo missing", gh_out="gh: Server Error (HTTP 502)", gh_rc=1)
        self.assertNotIn("missing", r.stdout)
        self.assertIn("::error title=GitHub API not reachable", r.stdout)

    def test_failing_tests_in_the_image_stop_the_run(self):
        """Regression (Review 2.20.1): the tests ran through "| tee" – without pipefail their failure was lost."""
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            for rc, expect_fail in ((1, True), (0, False)):
                f = Path(tmp) / "docker"
                f.write_text(f"#!/bin/sh\necho 'FAILED (failures=1)'\nexit {rc}\n")
                f.chmod(0o755)
                env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", GITHUB_STEP_SUMMARY=str(summary))
                r = subprocess.run(["bash", "-e", "-c", f". '{ROOT / 'tools' / 'ci.sh'}'; test_in_image img /src; "
                                    "echo after"], cwd=tmp, env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode != 0, expect_fail, r.stdout + r.stderr)
                self.assertEqual("after" in r.stdout, not expect_fail)
                if expect_fail:
                    self.assertIn("::error title=Tests failed in the image", r.stdout)

    def test_workflows_run_with_bash_pipefail(self):
        for name in ("build.yml", "security-update.yml", "scan.yml", "foundation.yml"):
            self.assertIn("defaults:\n  run:\n    shell: bash\n", workflow(name), name)

    def test_highest_release_is_by_number(self):
        r = self.run_sh("highest_release", gh_out="v2.18.4\nv2.19.0\nv2.9.0")
        self.assertEqual(r.stdout.strip(), "2.19.0")


class FakeTrivy:
    """Stands in for Trivy: writes a prepared JSON report and records the arguments."""

    def __init__(self, tmp: Path, report: dict, rc: int = 0):
        self.args = tmp / "args.txt"
        self.bin = tmp / "trivy"
        self.bin.write_text("#!/bin/sh\n"
                            f'printf "%s\\n" "$@" > "{self.args}"\n'
                            'while [ $# -gt 0 ]; do [ "$1" = --output ] && out="$2"; shift; done\n'
                            f"cat > \"$out\" <<'JSON'\n{json.dumps(report)}\nJSON\nexit {rc}\n")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)


def vuln(sev, vid, fixed="1.2.3"):
    return {"Severity": sev, "VulnerabilityID": vid, "PkgName": "libfoo", "InstalledVersion": "1.0",
            "FixedVersion": fixed, "Title": "test"}


class TrivyFlowTests(unittest.TestCase):
    def run_scan(self, report, rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = FakeTrivy(tmp, report, rc)
            summary = tmp / "summary.md"
            out = tmp / "trivy.json"
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                    mock.patch("sys.stdout") as stdout:
                code = trivy_scan.scan("image:test", binary=str(fake.bin), out=out)
            printed = "".join(c.args[0] for c in stdout.write.call_args_list)
            return code, printed, summary.read_text() if summary.exists() else "", fake.args.read_text()

    def test_blocking_finding_stops_release(self):
        code, printed, summ, args = self.run_scan({"Results": [{"Target": "x", "Vulnerabilities": [
            vuln("CRITICAL", "CVE-1"), vuln("HIGH", "CVE-2")]}]})
        self.assertEqual(code, 1)
        self.assertIn("::error title=Trivy: 2 blocking finding(s) – nothing is published::", printed)
        self.assertIn("CVE-1", summ)
        for a in ("--ignore-unfixed", "HIGH,CRITICAL", "--ignorefile"):
            self.assertIn(a, args)

    def test_unfixed_and_lower_findings_do_not_block(self):
        code, _, summ, _ = self.run_scan({"Results": [{"Target": "x", "Vulnerabilities": [
            vuln("HIGH", "CVE-3", fixed=""), vuln("MEDIUM", "CVE-4")]}]})
        self.assertEqual(code, 0)
        self.assertIn("0 blocking finding(s)", summ)

    def test_secret_blocks(self):
        code, _, _, _ = self.run_scan({"Results": [{"Target": "/app/x", "Secrets": [
            {"Severity": "HIGH", "RuleID": "aws-key", "Title": "AWS key"}]}]})
        self.assertEqual(code, 1)

    def test_trivy_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "trivy"
            bad.write_text("#!/bin/sh\nexit 2\n")
            bad.chmod(0o755)
            with mock.patch("sys.stdout") as stdout:
                self.assertEqual(trivy_scan.scan("x", binary=str(bad), out=Path(tmp) / "o.json"), 1)
            self.assertIn("Trivy could not scan", "".join(c.args[0] for c in stdout.write.call_args_list))

    def test_download_checked_against_sha256(self):
        lockdata = trivy_scan.read_lock()
        self.assertEqual(lockdata["version"], "0.76.0")
        self.assertRegex(lockdata["sha256"], r"^[0-9a-f]{64}$")
        with self.assertRaises(SystemExit), mock.patch("sys.stdout"):
            trivy_scan.install(lockdata, fetch=lambda url, dest: Path(dest).write_bytes(b"not trivy"))

    def test_install_success_path(self):
        import hashlib
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"#!/bin/sh\necho trivy\n"
            info = tarfile.TarInfo("trivy")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        blob = buf.getvalue()
        lockdata = dict(trivy_scan.read_lock(), sha256=hashlib.sha256(blob).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(trivy_scan, "INSTALL_DIR", Path(tmp)), \
                mock.patch("sys.stdout"):
            binary = trivy_scan.install(lockdata, fetch=lambda url, dest: Path(dest).write_bytes(blob))
            self.assertEqual(subprocess.run([str(binary)], capture_output=True, text=True).stdout.strip(), "trivy")

    def test_ignore_file_needs_reason_and_expiry(self):
        today = dt.date(2026, 10, 25)
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / ".trivyignore"
            f.write_text("# libfoo nur beim Bau benutzt – geprüft 2026-10-25\nCVE-2026-1 exp:2027-01-31\n")
            self.assertEqual(trivy_scan.check_ignore(f, today), [])
            f.write_text("CVE-2026-1 exp:2027-01-31\n")
            self.assertIn("reason missing", " ".join(trivy_scan.check_ignore(f, today)))
            f.write_text("# Grund\nCVE-2026-1\n")
            self.assertIn("exp:YYYY-MM-DD", " ".join(trivy_scan.check_ignore(f, today)))
            f.write_text("# Grund\nCVE-2026-1 exp:2028-01-01\n")
            self.assertIn("more than 6 months", " ".join(trivy_scan.check_ignore(f, today)))
            f.write_text("# Grund\nCVE-2026-1 exp:2026-10-01\n")
            self.assertIn("expired", " ".join(trivy_scan.check_ignore(f, today)))
        self.assertEqual(trivy_scan.check_ignore(), [])          # the real file is valid


class SecurityUpdateTests(unittest.TestCase):
    OLD = "base python:3.13.16-slim-trixie@sha256:aa\npython 3.13.16\ndeb libssl3t64 3.5.1-1\ndeb tzdata 2026a-1\n" \
          "pip certifi 2026.7.22\npip flask 3.1.3\n"
    NEW = "base python:3.13.17-slim-trixie@sha256:bb\npython 3.13.17\ndeb libssl3t64 3.5.1-1+deb13u1\n" \
          "deb tzdata 2026a-1\npip certifi 2026.10.19\npip flask 3.1.3\npip new 1.0\n"

    def test_nothing_changed_means_nothing_to_do(self):
        self.assertEqual(security_update.diff(self.OLD, self.OLD), [])
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            a.write_text(self.OLD)
            with mock.patch("sys.stdout"):
                self.assertEqual(security_update.main(["diff", str(a), str(a)]), security_update.UNCHANGED)

    def test_changes_listed_readably(self):
        lines = security_update.format_changes(security_update.diff(self.OLD, self.NEW))
        text = "\n".join(lines)
        self.assertIn("Debian-Pakete: `libssl3t64 3.5.1-1 → 3.5.1-1+deb13u1`", text)
        self.assertIn("`certifi 2026.7.22 → 2026.10.19`", text)
        self.assertIn("`new neu 1.0`", text)
        self.assertIn("Python: `3.13.16 → 3.13.17`", text)
        self.assertNotIn("tzdata", text)
        self.assertNotIn("flask", text)

    def test_counter_per_program_version(self):
        log = "# Änderungen\n\n## 2.18.3-s2 – 02.11.2026 (Sicherheitsupdate)\n\n## 2.18.3-s1 – 26.10.2026\n\n" \
              "## 2.18.3 – 24.10.2026\n## 2.18.2-s1 – 20.10.2026\n"
        self.assertEqual(security_update.next_tag("2.18.3", log), "2.18.3-s3")
        self.assertEqual(security_update.next_tag("2.19.0", log), "2.19.0-s1")   # new version: counter restarts

    def test_changelog_entry_on_top(self):
        entry = security_update.changelog_entry("2.18.3-s1", "2.18.3", security_update.diff(self.OLD, self.NEW),
                                                dt.date(2026, 10, 26))
        self.assertTrue(entry.startswith("## 2.18.3-s1 – 26.10.2026 (Sicherheitsupdate)\n"))
        self.assertIn("Gleiches Programm wie 2.18.3", entry)
        text = security_update.insert_changelog(entry, "# Änderungen\n\nIntro\n\n## 2.18.3 – x\n- a\n")
        self.assertLess(text.index("2.18.3-s1"), text.index("## 2.18.3 –"))
        self.assertTrue(text.startswith("# Änderungen"))

    def test_first_entry_without_old_package_list(self):
        entry = security_update.changelog_entry("2.19.0-s1", "2.19.0", security_update.diff("", self.NEW),
                                                dt.date(2026, 10, 26), first=True)
        self.assertIn("Erster Stand mit vollständiger Paketliste (2 Debian-Pakete, 3 Python-Pakete", entry)
        self.assertNotIn("libssl3t64", entry)

    def test_english_changelog_entry(self):
        entry = security_update.changelog_entry("2.18.3-s1", "2.18.3", security_update.diff(self.OLD, self.NEW),
                                                dt.date(2026, 10, 26), lang="en")
        self.assertTrue(entry.startswith("## 2.18.3-s1 – 2026-10-26 (security update)\n"))
        self.assertIn("Same program as 2.18.3", entry)
        self.assertIn("Debian packages: `libssl3t64 3.5.1-1 → 3.5.1-1+deb13u1`", entry)
        self.assertIn("`new new 1.0`", entry)
        first = security_update.changelog_entry("2.19.0-s1", "2.19.0", security_update.diff("", self.NEW),
                                                dt.date(2026, 10, 26), first=True, lang="en")
        self.assertIn("First state with a complete package list (2 Debian packages, 3 Python packages", first)

    def test_changelog_command_writes_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "old").write_text(self.OLD)
            (d / "new").write_text(self.NEW)
            de, en = d / "CHANGELOG.md", d / "CHANGELOG.en.md"
            de.write_text("# Änderungen\n\n## 2.18.3 – 24.10.2026\n")
            en.write_text("# Catalytix changes\n\n## 2.18.3 – 2026-10-24\n")
            with mock.patch.object(security_update, "CHANGELOG", de), \
                    mock.patch.object(security_update, "CHANGELOG_EN", en):
                self.assertEqual(security_update.main(["changelog", "2.18.3-s1", "2.18.3", str(d / "old"),
                                                       str(d / "new")]), 0)
            self.assertIn("(Sicherheitsupdate)", de.read_text())
            self.assertIn("(security update)", en.read_text())
            self.assertLess(en.read_text().index("2.18.3-s1"), en.read_text().index("## 2.18.3 –"))

    def test_workflow_commits_both_changelogs(self):
        wf = (ROOT / ".github" / "workflows" / "security-update.yml").read_text(encoding="utf-8")
        self.assertIn("CHANGELOG.md CHANGELOG.en.md", wf)

    def test_highest_ignores_security_tags(self):
        with mock.patch("sys.stdin") as stdin, mock.patch("sys.stdout") as stdout:
            stdin.read.return_value = "v2.19.0\nv2.20.0-s1\nv2.18.4\nnonsense"
            self.assertEqual(security_update.main(["highest"]), 0)
        self.assertEqual("".join(c.args[0] for c in stdout.write.call_args_list).strip(), "2.19.0")

    def test_latest_only_for_highest(self):
        self.assertEqual(security_update.main(["is-highest", "2.18.3", "2.18.3"]), 0)
        self.assertEqual(security_update.main(["is-highest", "2.18.3", "2.19.0"]), 1)
        self.assertEqual(security_update.main(["is-highest", "2.20.0", "v2.19.9"]), 0)


class FoundationTests(unittest.TestCase):
    def test_thresholds(self):
        today = dt.date(2026, 10, 25)
        with mock.patch("sys.stdout") as stdout:
            code, _ = foundation_check.evaluate([("Debian", "13", dt.date(2028, 8, 9))], today)
        self.assertEqual(code, 0)
        self.assertNotIn("::", "".join(c.args[0] for c in stdout.write.call_args_list))
        with mock.patch("sys.stdout") as stdout:
            code, _ = foundation_check.evaluate([("Python", "3.13", dt.date(2027, 9, 1))], today)
        self.assertEqual(code, 0)
        self.assertIn("::warning", "".join(c.args[0] for c in stdout.write.call_args_list))
        with mock.patch("sys.stdout") as stdout:
            code, _ = foundation_check.evaluate([("Python", "3.13", dt.date(2027, 3, 1))], today)
        self.assertEqual(code, 1)
        self.assertIn("::error", "".join(c.args[0] for c in stdout.write.call_args_list))

    def test_reads_foundation_and_both_apis(self):
        self.assertEqual(foundation_check.foundation(), [("Debian", "debian", "13"), ("Python", "python", "3.13")])

        def v1(url):
            return {"result": {"eolFrom": "2028-08-09"}}
        self.assertEqual(foundation_check.end_of_security_support("debian", "13", v1), dt.date(2028, 8, 9))

        def legacy(url):
            if "/v1/" in url:
                raise OSError("not found")
            return {"eol": "2029-10-31"}
        self.assertEqual(foundation_check.end_of_security_support("python", "3.13", legacy), dt.date(2029, 10, 31))

    def test_unreachable_service_only_warns(self):
        def down(url):
            raise OSError("offline")
        with mock.patch("sys.stdout") as stdout:
            self.assertEqual(foundation_check.main(dt.date(2026, 10, 25), fetch=down), 0)
        self.assertIn("::warning title=Foundation check not possible", "".join(
            c.args[0] for c in stdout.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
