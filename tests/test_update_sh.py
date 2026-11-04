"""update.sh (from 3.0.0): pull, start, wait for the health check, roll back on failure; store a newer
docker-compose.yml; without internet; cleanup of old installations. With a fake docker (fake_docker.py),
run with dash as on a NAS. Everything in temporary folders."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG = "ghcr.io/soleil0815/catalytix"
SH = shutil.which("dash") or "sh"

COMPOSE_V2 = (ROOT / "docker-compose.yml").read_text()
COMPOSE_V3 = COMPOSE_V2.replace('CATALYTIX_COMPOSE: "2"', 'CATALYTIX_COMPOSE: "3"')
COMPOSE_OLD = 'services:\n  catalytix:\n    build: .\n    image: "${CATALYTIX_IMAGE:-catalytix}:${CATALYTIX_TAG:-2.20.1}"\n'


def img(version, compose=COMPOSE_V2, update_sh=None):
    return {"id": f"sha256:{version}", "version": version,
            "dist": {"docker-compose.yml": compose, ".env.example": "#CATALYTIX_TAG=latest\n",
                     "update.sh": update_sh if update_sh is not None else (ROOT / "update.sh").read_text()}}


class Base(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="cx-update-"))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        for name in ("docker", "docker-compose"):
            (self.bin / name).write_text(f"#!{sys.executable}\n" + (ROOT / "tests" / "fake_docker.py").read_text())
            (self.bin / name).chmod(0o755)
        self.state_file = self.t / "state.json"
        self.log = self.t / "docker.log"
        self.inst = self.t / "inst"
        self.inst.mkdir()
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        self.state = {"images": {}, "registry": {}, "container": None, "health": {}, "run_rc": {}, "mounts": []}

    # --- Helpers
    def installed(self, version="3.0.0", tag=None, compose=COMPOSE_V2, env="#CATALYTIX_PORT=9300\n"):
        """Running installation: container with <version>, docker-compose.yml, .env, data/."""
        ref = f"{IMG}:{tag or 'latest'}"
        self.state["images"][ref] = img(version)
        self.state["container"] = {"image": f"sha256:{version}", "status": "running", "health": "healthy",
                                   "restarts": 0, "project": ""}
        (self.inst / "docker-compose.yml").write_text(compose)
        (self.inst / ".env").write_text(env)
        (self.inst / "data").mkdir()
        (self.inst / "data" / "catalytix.db").write_text("DATA")

    def publish(self, version, tags=("latest",), **kw):
        for tag in tags:
            self.state["registry"][f"{IMG}:{tag}"] = img(version, **kw)
        self.state["registry"][f"{IMG}:{version}"] = img(version, **kw)

    def run_update(self, *args, tty=False, env=None):
        self.state_file.write_text(json.dumps(self.state))
        # Only the fake docker – a real one on the machine is never called (python3 for fake_docker.py)
        e = dict(os.environ, PATH=f"{self.bin}:{Path(sys.executable).parent}:/usr/bin:/bin",
                 FAKE_DOCKER_STATE=str(self.state_file),
                 FAKE_DOCKER_LOG=str(self.log), CATALYTIX_HEALTH_POLL="0", CATALYTIX_HEALTH_TIMEOUT="5")
        e.pop("CATALYTIX_TAG", None)
        e.pop("CATALYTIX_IMAGE", None)
        e.update(env or {})
        r = subprocess.run([SH, str(self.inst / "update.sh"), *args], cwd=self.t, env=e, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.state = json.loads(self.state_file.read_text())
        self.out = r.stdout + r.stderr
        return r

    def docker_log(self):
        return self.log.read_text() if self.log.exists() else ""

    def env(self):
        return (self.inst / ".env").read_text()

    def running_version(self):
        c = self.state["container"]
        return c and c["image"].split(":", 1)[1]


class UpdateTests(Base):
    def test_fresh_install_with_only_update_sh(self):
        self.publish("3.0.0")
        r = self.run_update()
        self.assertEqual(r.returncode, 0, self.out)
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), COMPOSE_V2)
        self.assertTrue((self.inst / ".env").exists())
        self.assertEqual(os.stat(self.inst / ".env").st_mode & 0o077, 0)        # owner only
        self.assertFalse((self.inst / "docker-compose.yml.alt").exists())
        self.assertEqual(self.running_version(), "3.0.0")
        self.assertIn("Version 3.0.0 is running", self.out)

    def test_normal_update_keeps_env_and_data(self):
        self.installed("3.0.0")
        self.publish("3.1.0", update_sh="#!/bin/sh\n# new update.sh\n")
        r = self.run_update()
        self.assertEqual(r.returncode, 0, self.out)
        self.assertEqual(self.running_version(), "3.1.0")
        self.assertEqual(self.env(), "#CATALYTIX_PORT=9300\n")                   # nothing written to .env
        self.assertEqual((self.inst / "data" / "catalytix.db").read_text(), "DATA")
        self.assertIn(f"docker tag sha256:3.0.0 {IMG}:previous", self.docker_log())   # way back recorded
        self.assertIn(f"docker pull {IMG}:latest", self.docker_log())
        self.assertFalse((self.inst / "docker-compose.yml.alt").exists())       # same compose version
        self.assertTrue((self.inst / "update.sh").read_text().endswith("# new update.sh\n"))  # update.sh from the image
        self.assertIn("./update.sh --version=3.0.0", self.out)
        self.assertEqual(list(self.inst.glob(".catalytix-*")), [])               # no leftovers

    def test_newer_compose_file_placed_old_one_kept(self):
        self.installed("3.0.0", compose=COMPOSE_OLD)
        self.publish("3.1.0", compose=COMPOSE_V3)
        r = self.run_update()
        self.assertEqual(r.returncode, 0, self.out)
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), COMPOSE_V3)
        self.assertEqual((self.inst / "docker-compose.yml.alt").read_text(), COMPOSE_OLD)

    def test_older_compose_in_image_does_not_replace(self):
        self.installed("3.1.0", compose=COMPOSE_V3)
        self.publish("3.1.1", compose=COMPOSE_V2)
        self.run_update()
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), COMPOSE_V3)

    def test_version_is_written_to_env(self):
        self.installed("3.0.0")
        self.publish("3.0.1", tags=("3.0.1-s2",))
        r = self.run_update("--version=3.0.1-s2")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn("CATALYTIX_TAG=3.0.1-s2\n", self.env())
        self.assertIn(f"docker pull {IMG}:3.0.1-s2", self.docker_log())
        self.publish("3.2.0")
        self.log.unlink()
        r = self.run_update()                                                   # stays on the pinned version
        self.assertIn(f"docker pull {IMG}:3.0.1-s2", self.docker_log())
        self.assertNotIn(f"docker pull {IMG}:latest", self.docker_log())
        r = self.run_update("--version=latest")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn("CATALYTIX_TAG=latest\n", self.env())
        self.assertEqual(self.running_version(), "3.2.0")

    def test_invalid_versions_rejected(self):
        self.installed("3.0.0")
        for bad in ("3.0", "3.0.0-x1", "3.0.0-s", "latest-s1", "../x", "3.0.0;rm"):
            r = self.run_update(f"--version={bad}")
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("expects a version", self.out)
        self.assertNotIn("docker pull", self.docker_log())

    def test_pull_failure_changes_nothing(self):
        self.installed("3.0.0")
        r = self.run_update("--version=9.9.9")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing changed", self.out)
        self.assertEqual(self.env(), "#CATALYTIX_PORT=9300\n")
        self.assertNotIn(" up ", self.docker_log())
        self.assertEqual(self.running_version(), "3.0.0")

    def test_old_settings_tidied(self):
        self.installed("2.20.1", env="CATALYTIX_IMAGE=catalytix\nCATALYTIX_PINNED=1\nX=1\n")
        self.publish("3.0.0")
        r = self.run_update()
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn("#CATALYTIX_PINNED=1", self.env())
        self.assertIn("#CATALYTIX_IMAGE=catalytix", self.env())
        self.assertIn("X=1", self.env())

    def test_project_name_of_running_container_is_kept(self):
        self.installed("3.0.0")
        self.state["container"]["project"] = "bib-prod"
        self.publish("3.1.0")
        self.run_update()
        self.assertIn("docker compose -p bib-prod up -d", self.docker_log())

    def test_old_pull_spelling_still_works(self):
        # The old script's hint reads "./update.sh --pull=X.Y.Z" (Review 3.0.0)
        self.installed("3.0.0")
        self.publish("3.0.1")
        r = self.run_update("--pull=3.0.1")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn("CATALYTIX_TAG=3.0.1", self.env())
        r = self.run_update("--pull")
        self.assertIn("CATALYTIX_TAG=latest", self.env())

    def test_image_without_health_check(self):
        self.installed("3.0.0")
        self.publish("3.1.0")
        self.state["health"]["3.1.0"] = "none"
        r = self.run_update(env={"CATALYTIX_HEALTH_NOCHECK": "2"})
        self.assertEqual(r.returncode, 0, self.out)
        self.assertEqual(self.running_version(), "3.1.0")

    def test_old_docker_compose_v1(self):
        self.installed("3.0.0")
        self.state["compose_v2"] = False
        self.publish("3.1.0")
        r = self.run_update()
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn("docker-compose up -d", self.docker_log())


class RollbackTests(Base):
    def setUp(self):
        super().setUp()
        self.installed("3.0.0")
        self.publish("3.1.0", compose=COMPOSE_V3)
        self.state["health"]["3.1.0"] = "unhealthy"

    def test_failed_start_goes_back_and_restores_database(self):
        r = self.run_update("-y")
        self.assertNotEqual(r.returncode, 0)
        log = self.docker_log()
        # restore backup "before-3.1.0" from THIS run with the previous image, check disabled for it
        self.assertRegex(log, r"run --rm --no-deps -e CATALYTIX_SKIP_DB_CHECK=1 catalytix flask --app wsgi "
                              r"restore-before 3\.1\.0 --since \d+ --yes  \[image=" + IMG + ":previous]")
        self.assertIn("RUN version=3.0.0 flask --app wsgi restore-before 3.1.0", log)
        self.assertEqual(self.running_version(), "3.0.0")
        self.assertIn("CATALYTIX_TAG=3.0.0", self.env())       # stays until there is a new version
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), COMPOSE_V2)   # old file back
        self.assertIn("Back on version 3.0.0", self.out)
        self.assertIn("Traceback", self.out)                     # reason is shown

    def test_pinned_version_stays_pinned(self):
        (self.inst / ".env").write_text("CATALYTIX_TAG=3.0.0\n")
        self.state["images"][f"{IMG}:3.0.0"] = img("3.0.0")
        self.run_update("-y", "--version=3.1.0")
        self.assertEqual(self.env(), "CATALYTIX_TAG=3.0.0\n")
        self.assertEqual(self.running_version(), "3.0.0")

    def test_without_terminal_and_without_yes_nothing_is_decided(self):
        r = self.run_update()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("restore-before", self.docker_log())
        self.assertIn("no input possible", self.out)

    def test_previous_before_3_restores_with_the_new_image(self):
        # 2.x does not know restore-before – the new image restores the 3.x backup (Review 3.0.0)
        self.state["images"] = {f"{IMG}:latest": img("2.20.1")}
        self.state["container"]["image"] = "sha256:2.20.1"
        self.run_update("-y")
        self.assertIn("RUN version=3.1.0 flask --app wsgi restore-before 3.1.0", self.docker_log())
        self.assertEqual(self.running_version(), "2.20.1")

    def test_security_update_tag_is_kept_on_the_way_back(self):
        self.state["images"][f"{IMG}:3.0.0-s2"] = self.state["images"][f"{IMG}:latest"]
        self.state["images"][f"{IMG}:3.0.0-s10"] = self.state["images"][f"{IMG}:latest"]
        self.run_update("-y")
        self.assertIn("CATALYTIX_TAG=3.0.0-s10", self.env())          # not the older release image 3.0.0
        self.assertEqual(self.running_version(), "3.0.0")

    def test_mysql_without_backup_is_reported(self):
        self.state["run_rc"]["restore-before"] = 4
        self.run_update("-y")
        self.assertIn("restore your dump", self.out)

    def test_crash_loop_counts_as_failure(self):
        self.state["health"]["3.1.0"] = "exited"
        r = self.run_update("-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.running_version(), "3.0.0")


class GoingBackTests(Base):
    def test_older_version_restores_matching_backup_first(self):
        self.installed("3.1.0")
        self.publish("3.0.0")
        r = self.run_update("-y", "--version=3.0.0")
        self.assertEqual(r.returncode, 0, self.out)
        log = self.docker_log()
        self.assertIn("RUN version=3.1.0 flask --app wsgi restore-for-version 3.0.0 --yes", log)
        self.assertLess(log.index("restore-for-version"), log.rindex(" up -d"))
        self.assertIn("CATALYTIX_TAG=3.0.0", self.env())
        self.assertEqual(self.running_version(), "3.0.0")

    def test_no_usable_backup_aborts(self):
        self.installed("3.1.0")
        self.publish("3.0.0")
        self.state["run_rc"]["restore-for-version"] = 3
        r = self.run_update("-y", "--version=3.0.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Restore your own dump", self.out)
        self.assertEqual(self.env(), "#CATALYTIX_PORT=9300\n")
        self.assertEqual(self.running_version(), "3.1.0")

    def test_failed_downgrade_puts_the_newer_data_back(self):
        # Regression (Review 3.0.0): 3.0.0 does not start → 3.1.0 must get ITS data back
        self.installed("3.1.0")
        self.publish("3.0.0")
        self.state["health"]["3.0.0"] = "unhealthy"
        self.state["run_out"] = {"restore-for-version":
                                 "Previous state backed up as catalytix-2026-10-29_030000-vor-wiederherstellung.db"}
        r = self.run_update("-y", "--version=3.0.0")
        self.assertNotEqual(r.returncode, 0)
        log = self.docker_log()
        self.assertIn("RUN version=3.1.0 flask --app wsgi restore-backup "
                      "catalytix-2026-10-29_030000-vor-wiederherstellung.db --yes", log)
        self.assertNotIn("restore-before", log)
        self.assertEqual(self.running_version(), "3.1.0")
        self.assertEqual(self.env(), "#CATALYTIX_PORT=9300\nCATALYTIX_TAG=3.1.0\n")

    def test_going_back_keeps_the_newer_update_sh(self):
        self.installed("3.1.0")
        self.publish("3.0.0", update_sh="#!/bin/sh\n# old update.sh\n")
        before = (self.inst / "update.sh").read_text()
        r = self.run_update("-y", "--version=3.0.0")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertEqual((self.inst / "update.sh").read_text(), before)

    def test_asks_before_going_back(self):
        self.installed("3.1.0")
        self.publish("3.0.0")
        r = self.run_update("--version=3.0.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("restore-for-version", self.docker_log())


class OfflineTests(Base):
    def image_file(self, version, good_sum=True, sums="SHA256SUMS"):
        d = self.t / "release"
        d.mkdir(exist_ok=True)
        f = d / f"catalytix-{version}-amd64.tar.gz"
        f.write_text(json.dumps({"ref": f"{IMG}:{version}", "image": img(version)}))
        digest = hashlib.sha256(f.read_bytes()).hexdigest() if good_sum else "0" * 64
        if sums == "SHA256SUMS":
            (d / "SHA256SUMS").write_text(f"{'1' * 64}  update.sh\n{digest}  {f.name}\n")
        elif sums == ".sha256":
            Path(str(f) + ".sha256").write_text(f"{digest}  {f.name}\n")
        return f

    def test_load_with_checksum(self):
        self.installed("3.0.0")
        f = self.image_file("3.1.0")
        r = self.run_update("--load", str(f))
        self.assertEqual(r.returncode, 0, self.out)
        self.assertNotIn("docker pull", self.docker_log())
        self.assertIn("Checksum matches", self.out)
        self.assertIn("CATALYTIX_TAG=3.1.0", self.env())       # without internet: exactly this version
        self.assertEqual(self.running_version(), "3.1.0")

    def test_load_with_sha256_file_and_relative_path(self):
        self.installed("3.0.0")
        f = self.image_file("3.1.0", sums=".sha256")
        r = self.run_update("--load=release/" + f.name)          # relative to the calling directory
        self.assertEqual(r.returncode, 0, self.out)

    def test_load_with_own_registry_name(self):
        self.installed("3.0.0", env="CATALYTIX_IMAGE=registry.firma.local/catalytix\n")
        self.state["images"]["registry.firma.local/catalytix:latest"] = img("3.0.0")
        f = self.image_file("3.1.0")
        r = self.run_update("--load", str(f))
        self.assertEqual(r.returncode, 0, self.out)
        self.assertIn(f"docker tag {IMG}:3.1.0 registry.firma.local/catalytix:3.1.0", self.docker_log())
        self.assertIn("[image=registry.firma.local/catalytix:3.1.0]", self.docker_log())
        self.assertEqual(self.running_version(), "3.1.0")

    def test_wrong_checksum_changes_nothing(self):
        self.installed("3.0.0")
        f = self.image_file("3.1.0", good_sum=False)
        r = self.run_update("--load", str(f))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does NOT match", self.out)
        self.assertNotIn("docker load", self.docker_log())
        self.assertEqual(self.running_version(), "3.0.0")

    def test_missing_checksum_needs_confirmation(self):
        self.installed("3.0.0")
        f = self.image_file("3.1.0", sums=None)
        r = self.run_update("--load", str(f))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("docker load", self.docker_log())


class CleanupTests(Base):
    def setUp(self):
        super().setUp()
        self.installed("3.0.0", env="SECRET=1\n")
        i = self.inst
        (i / "catalytix").mkdir()
        (i / "catalytix" / "__init__.py").write_text('__version__ = "2.20.1"\n')
        for f in ("Dockerfile", "wsgi.py", "README.md", "catalytix-2.20.1.zip", "catalytix-2.20.1.zip.sha256",
                  "docker-compose.override.yml", "docker-compose.yml.alt"):
            (i / f).write_text("# Catalytix\n")
        (i / "requirements.txt").write_text("Flask==3.1.2\n")
        (i / ".catalytix-update.abc123").mkdir()
        (i / "data" / "backups").mkdir()
        (i / "data" / "backups" / "b.db").write_text("B")
        self.outside = self.t / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("K")
        self.state["images"]["catalytix:2.20.1"] = img("2.20.1")

    def test_lists_and_asks_without_deleting(self):
        r = self.run_update("--cleanup")
        self.assertEqual(r.returncode, 0, self.out)
        for name in ("catalytix", "Dockerfile", "catalytix-2.20.1.zip", ".catalytix-update.abc123",
                     "image catalytix:2.20.1"):
            self.assertIn(name, self.out)
        self.assertIn("Nothing deleted", self.out)
        self.assertTrue((self.inst / "catalytix").exists())

    def test_deletes_only_program_files(self):
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, self.out)
        i = self.inst
        for gone in ("catalytix", "Dockerfile", "wsgi.py", "requirements.txt", "README.md", "catalytix-2.20.1.zip",
                     "catalytix-2.20.1.zip.sha256", ".catalytix-update.abc123"):
            self.assertFalse((i / gone).exists(), gone)
        for kept in ("data/catalytix.db", "data/backups/b.db", ".env", "docker-compose.yml",
                     "docker-compose.override.yml", "docker-compose.yml.alt", "update.sh"):
            self.assertTrue((i / kept).exists(), kept)
        self.assertEqual((i / ".env").read_text(), "SECRET=1\n")
        self.assertIn("docker image rm catalytix:2.20.1", self.docker_log())
        self.assertEqual(list(i.glob(".catalytix-protect*")) + list(i.glob(".catalytix-list*")), [])

    def test_links_to_data_are_never_followed(self):
        shutil.rmtree(self.inst / "catalytix")
        (self.inst / "catalytix").symlink_to(self.inst / "data")            # link to the data
        (self.inst / "docs").symlink_to(self.inst)                          # link to the whole folder
        (self.inst / "tools").symlink_to(self.outside)                      # link to outside
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertTrue((self.inst / "data" / "catalytix.db").exists())
        self.assertTrue((self.inst / "data" / "backups" / "b.db").exists())
        self.assertTrue((self.inst / "catalytix").is_symlink())           # skipped
        self.assertTrue((self.inst / "docs").is_symlink())
        self.assertIn("skipped", self.out)
        self.assertFalse((self.inst / "tools").is_symlink() or (self.inst / "tools").exists())  # only the link removed
        self.assertEqual((self.outside / "keep.txt").read_text(), "K")

    def test_folders_mounted_by_containers_are_protected(self):
        (self.inst / "docs").mkdir()
        (self.inst / "docs" / "x").write_text("x")
        self.state["mounts"] = [str(self.inst / "docs" / "x")]             # some container mounts something inside it
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertTrue((self.inst / "docs" / "x").exists())
        self.assertIn("skipped", self.out)

    def test_backup_folder_with_symlink_into_program_folder(self):
        # data/backups points to a folder named like a program folder
        shutil.rmtree(self.inst / "data" / "backups")
        (self.inst / "tests").mkdir()
        (self.inst / "tests" / "b.db").write_text("B")
        (self.inst / "data" / "backups").symlink_to(self.inst / "tests")
        self.run_update("--cleanup", "-y")
        self.assertTrue((self.inst / "tests" / "b.db").exists())

    def test_file_name_with_line_break_cannot_reach_data(self):
        (self.inst / "catalytix-\ndata\n.zip").write_text("x")             # Regression (Review 3.0.0)
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertTrue((self.inst / "data" / "catalytix.db").exists())
        self.assertTrue((self.inst / "catalytix-\ndata\n.zip").exists())
        self.assertIn("unusual characters", self.out)

    def test_refuses_in_a_folder_that_is_not_catalytix(self):
        other = self.t / "home"
        other.mkdir()
        shutil.copy(ROOT / "update.sh", other / "update.sh")
        for d in ("docs", "tools", "tests", "docker"):
            (other / d).mkdir()
            (other / d / "mine.txt").write_text("mine")
        (other / "README.md").write_text("my notes")
        self.inst = other
        r = self.run_update("--cleanup", "-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No old Catalytix installation", self.out)
        for d in ("docs", "tools", "tests", "docker"):
            self.assertTrue((other / d / "mine.txt").exists())

    def test_own_files_with_known_names_are_kept(self):
        (self.inst / "docs").mkdir()
        (self.inst / "docs" / "mine.txt").write_text("mine")              # not a manual – belongs to the user
        (self.inst / "README.md").write_text("my notes")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, self.out)
        self.assertTrue((self.inst / "docs" / "mine.txt").exists())
        self.assertTrue((self.inst / "README.md").exists())
        self.assertIn("does not look like a Catalytix program file", self.out)

    def test_linked_data_folder_with_link_inside(self):
        ext = self.t / "ext"
        shutil.move(str(self.inst / "data"), ext)
        (self.inst / "data").symlink_to(ext)                                # data → /ext
        (self.inst / "tools").mkdir()
        (self.inst / "tools" / "lock.py").write_text("x")
        (self.inst / "tools" / "backups").mkdir()
        (self.inst / "tools" / "backups" / "b2.db").write_text("B")
        shutil.rmtree(ext / "backups")
        (ext / "backups").symlink_to(self.inst / "tools" / "backups")      # /ext/backups → tools/backups
        self.run_update("--cleanup", "-y")
        self.assertTrue((self.inst / "tools" / "backups" / "b2.db").exists())

    def test_git_checkout_is_left_alone(self):
        (self.inst / ".git").mkdir()
        r = self.run_update("--cleanup", "-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.inst / "catalytix").exists())


class LayoutTests(unittest.TestCase):
    def test_every_grid_span_class_exists(self):
        # Regression (3.0.0): .span-1/.span-5 were missing – the name field in the group form was tiny
        import glob
        css = (ROOT / "catalytix" / "static" / "css" / "app.css").read_text()
        used = set()
        for f in glob.glob(str(ROOT / "catalytix" / "templates" / "**" / "*.html"), recursive=True):
            used |= set(re.findall(r"\bspan-(\d)\b", Path(f).read_text()))
        for n in sorted(used):
            self.assertIn(f".span-{n} {{", css, f"span-{n}")


class ScriptTests(unittest.TestCase):
    def test_posix_sh(self):
        text = (ROOT / "update.sh").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        subprocess.run([SH, "-n", str(ROOT / "update.sh")], check=True)
        for bashism in ("[[ ", "$'\\", "local -a", "${!", "<(", "pipefail", "BASH_VERSION", "declare "):
            self.assertNotIn(bashism, text, bashism)

    def test_no_zip_no_build_no_program_files(self):
        text = (ROOT / "update.sh").read_text()
        for gone in ("unzip", " build ", "zip_extract", "Swap the files"):
            self.assertNotIn(gone, text, gone)

    def test_versions_match(self):
        v = re.search(r'__version__ = "(.*)"', (ROOT / "catalytix" / "__init__.py").read_text()).group(1)
        self.assertIn(f'org.opencontainers.image.version="{v}"', (ROOT / "Dockerfile").read_text())

    def test_image_carries_host_files(self):
        self.assertIn("/app/dist/", (ROOT / "Dockerfile").read_text())
        ignore = (ROOT / ".dockerignore").read_text()
        for name in ("update.sh", "docker-compose.yml", ".env.example"):
            self.assertNotIn(name, ignore)

    def test_no_container_manager_labels(self):
        # container managers may update – no more lock labels (like the former dockhand.update=false)
        self.assertNotIn("labels:", (ROOT / "docker-compose.yml").read_text())

    def test_gitignore_keeps_secrets_out(self):
        gi = (ROOT / ".gitignore").read_text()
        for entry in ("data/", "instance/", ".env", "secret_key", "*.db"):
            self.assertIn(entry, gi)

    def test_executable_in_repo(self):
        self.assertTrue(os.stat(ROOT / "update.sh").st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
