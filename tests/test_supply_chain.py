"""Selbsttests für Bau und Auslieferung: feste Versionen, Grenzen des Unterbaus, Workflows, Trivy-Ablauf.

Ohne PyYAML (ist keine Abhängigkeit des Programms) – die Workflows werden als Text geprüft.
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
        self.assertFalse(lock.within("3.1.2", "3.1.3"))          # nie zurück
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
            "2026.10.19": [{"upload_time_iso_8601": "2026-10-19T10:00:00Z"}],   # 6 Tage – zu jung
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
        self.assertIn("upgrade --with-new-pkgs", text)            # auch die Pakete des Basis-Images
        self.assertIn("--require-hashes", text)
        self.assertIn("python -m pip uninstall -y pip", text)     # Paketmanager nicht im fertigen Image
        self.assertIn('! python -c "import pip"', text)

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
            self.assertEqual(lock.main(["same-build", a, b]), 0)        # nur Lock-Werte verschieden
            lock.copy_locks(Path(b), Path(a))
            self.assertIn("ARG DEBIAN_SNAPSHOT=20990101T000000Z", Path(a, "Dockerfile").read_text())
            Path(b, "requirements.in").write_text("Flask>=3.1,<4\nneu>=1,<2\n")
            self.assertEqual(lock.main(["same-build", a, b]), 1)        # unveröffentlichte Bau-Änderung


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
        # Schreibrechte nur in den veröffentlichenden Jobs
        for name, job in (("build.yml", "check"), ("security-update.yml", "update")):
            self.assertNotIn(": write", jobs(workflow(name))[job], f"{name}/{job}")

    def test_schedules(self):
        self.assertRegex(workflow("security-update.yml"), r'cron: "\d+ [0-6] \* \* 1"')      # montags früh
        self.assertRegex(workflow("scan.yml"), r'cron: "\d+ \d+ \* \* \*"')                  # täglich
        self.assertRegex(workflow("foundation.yml"), r'cron: "\d+ \d+ \* \* 1"')             # wöchentlich
        for name in ("security-update.yml", "scan.yml", "foundation.yml"):
            self.assertIn("workflow_dispatch:", workflow(name), name)                        # von Hand startbar

    def test_publishing_never_concurrent_and_only_from_main(self):
        self.assertIn("group: publish-${{ github.ref }}", workflow("build.yml"))
        sec = workflow("security-update.yml")
        self.assertIn("group: publish-refs/heads/main", sec)
        self.assertIn("cancel-in-progress: false", sec)
        self.assertIn("github.ref == 'refs/heads/main'", jobs(workflow("build.yml"))["publish"])
        self.assertIn("if: github.ref == 'refs/heads/main'", jobs(sec)["update"])

    def test_release_order(self):
        check = jobs(workflow("build.yml"))["check"]
        order = ["Check locks", "Build the image (without cache)", "Run the tests in the image", "Security scan"]
        positions = [check.index(o) for o in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--no-cache", check)
        publish = jobs(workflow("build.yml"))["publish"]
        self.assertIn("needs: check", publish)
        self.assertIn("never overwritten", publish)
        self.assertIn("is-highest", check)                                   # :latest nur für die höchste Version

    def test_security_update_order(self):
        text = workflow("security-update.yml")
        upd = jobs(text)["update"]
        order = ["Latest release", "same-build", "lock.py update", "--no-cache", "Compare with the published",
                 "Run the tests in the image", "trivy_scan.py scan", "upload-artifact"]
        positions = [upd.index(o) for o in order]
        self.assertEqual(positions, sorted(positions), order)
        self.assertIn("if: steps.compare.outputs.changed == 'true'", upd)     # nichts geändert: nichts tun
        pub = jobs(text)["publish"]
        self.assertIn("needs: update", pub)
        self.assertIn("if: needs.update.outputs.changed == 'true'", pub)
        order = ["main changed during the run", "is-highest", "--no-cache", "next-tag", "imagetools inspect",
                 "changelog", "git push", "--push"]
        positions = [pub.index(o) for o in order]
        self.assertEqual(positions, sorted(positions), order)
        self.assertIn('-t "$image:$TAG" -t "$image:latest"', pub)

    def test_errors_are_readable_on_the_run(self):
        for name in ("build.yml", "security-update.yml"):
            text = workflow(name)
            self.assertGreaterEqual(text.count("::error title="), 4, name)


class FakeTrivy:
    """Steht für Trivy: schreibt einen vorbereiteten JSON-Bericht und merkt sich die Argumente."""

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
        self.assertEqual(trivy_scan.check_ignore(), [])          # die echte Datei ist gültig


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
        self.assertEqual(security_update.next_tag("2.19.0", log), "2.19.0-s1")   # neue Version: Zähler neu

    def test_changelog_entry_on_top(self):
        entry = security_update.changelog_entry("2.18.3-s1", "2.18.3", security_update.diff(self.OLD, self.NEW),
                                                dt.date(2026, 10, 26))
        self.assertTrue(entry.startswith("## 2.18.3-s1 – 26.10.2026 (Sicherheitsupdate)\n"))
        self.assertIn("Gleiches Programm wie 2.18.3", entry)
        text = security_update.insert_changelog(entry, "# Änderungen\n\nIntro\n\n## 2.18.3 – x\n- a\n")
        self.assertLess(text.index("2.18.3-s1"), text.index("## 2.18.3 –"))
        self.assertTrue(text.startswith("# Änderungen"))

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
