"""update.sh räumt nach einem erfolgreichen Update auf: alte Images, alte Zip-Pakete, verwaiste Arbeitsordner."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.log = self.t / "docker.log"
        # Fake-docker: kennt einige Catalytix-Images, protokolliert alles
        (self.bin / "docker").write_text(
            '#!/bin/bash\necho "docker $*" >> "$LOG"\n'
            'case "$*" in\n'
            '  "compose ps -q catalytix") echo abc;;\n'
            '  "inspect -f {{.State.Status}} abc") echo running;;\n'
            '  *com.docker.compose.project*) echo "";;\n'
            '  inspect*) echo healthy;;\n'
            '  "image ls --format {{.Repository}}:{{.Tag}} catalytix") printf "%s\\n" $IMAGES;;\n'
            'esac\nexit 0\n')
        (self.bin / "docker").chmod(0o755)
        self.inst = self.t / "inst"
        self._tree(self.inst, "1.0.0")
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        (self.inst / ".env").write_text("")
        (self.inst / "data" / "__pycache__").mkdir(parents=True)          # in ./data: nie anfassen
        (self.inst / "catalytix" / "__pycache__").mkdir()
        (self.inst / "catalytix" / "__pycache__" / "x.pyc").write_text("x")
        # Pakete im Programmordner
        for v in ("0.8.0", "0.9.0", "1.0.0", "3.0.0"):
            (self.inst / f"catalytix-{v}.zip").write_text("alt" * 100)
        (self.inst / "catalytix-notizen.zip").write_text("eigene Datei")
        (self.inst / "catalytix-0.5.0.zip").symlink_to(self.inst / "catalytix-0.8.0.zip")
        # verwaiste Arbeitsordner: einer alt, einer frisch
        old = self.inst / ".catalytix-update.alt123"
        (old / "old").mkdir(parents=True)
        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        (self.inst / ".catalytix-update.neu456").mkdir()
        # neues Paket 2.0.0 in einem anderen Ordner, daneben noch ältere Pakete
        self.pkgdir = self.t / "downloads"
        self.pkgdir.mkdir()
        (self.pkgdir / "catalytix-0.9.0.zip").write_text("alt")
        (self.pkgdir / "catalytix-1.0.0.zip").write_text("alt")
        src = self.t / "src" / "catalytix"
        self._tree(src, "2.0.0")
        shutil.copy(ROOT / "update.sh", src / "update.sh")
        self.zip = self.pkgdir / "catalytix-2.0.0.zip"
        with zipfile.ZipFile(self.zip, "w") as z:
            for f in src.rglob("*"):
                z.write(f, f.relative_to(src.parent))

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    @staticmethod
    def _tree(d, version):
        (d / "catalytix").mkdir(parents=True)
        (d / "catalytix" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        for f in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (d / f).write_text("x\n")

    def run_update(self, *opts):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LOG=str(self.log),
                   IMAGES="catalytix:0.8.0 catalytix:0.9.0 catalytix:1.0.0 catalytix:2.0.0 catalytix:3.0.0 "
                          "catalytix:latest")
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", *opts, str(self.zip)],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def removed_images(self):
        return sorted(line.split()[-1] for line in self.log.read_text().splitlines()
                      if line.startswith("docker image rm "))

    def zips(self, d):
        return sorted(p.name for p in d.glob("catalytix-*.zip"))

    def test_default_keeps_current_previous_and_newer(self):
        out = self.run_update()
        self.assertEqual(self.removed_images(), ["catalytix:0.8.0", "catalytix:0.9.0"])   # „latest“ bleibt
        self.assertEqual(self.zips(self.inst), ["catalytix-0.5.0.zip", "catalytix-1.0.0.zip", "catalytix-3.0.0.zip",
                                                "catalytix-notizen.zip"])
        self.assertTrue((self.inst / "catalytix-0.5.0.zip").is_symlink())               # Verknüpfungen bleiben
        self.assertEqual(self.zips(self.pkgdir), ["catalytix-1.0.0.zip", "catalytix-2.0.0.zip"])
        self.assertFalse((self.inst / ".catalytix-update.alt123").exists())
        self.assertTrue((self.inst / ".catalytix-update.neu456").exists())
        self.assertIn("aborted update", out)
        self.assertFalse((self.inst / "catalytix" / "__pycache__").exists())
        self.assertTrue((self.inst / "data" / "__pycache__").exists())
        self.assertIn("way back", out)

    def test_prune_keeps_only_current(self):
        self.run_update("--prune")
        self.assertEqual(self.removed_images(), ["catalytix:0.8.0", "catalytix:0.9.0", "catalytix:1.0.0",
                                                 "catalytix:3.0.0"])
        self.assertEqual(self.zips(self.inst), ["catalytix-0.5.0.zip", "catalytix-notizen.zip"])
        self.assertEqual(self.zips(self.pkgdir), ["catalytix-2.0.0.zip"])

    def test_no_cleanup(self):
        self.run_update("--no-cleanup")
        self.assertEqual(self.removed_images(), [])
        self.assertEqual(len(self.zips(self.inst)), 6)
        self.assertTrue((self.inst / ".catalytix-update.alt123").exists())

    def test_nothing_removed_when_update_fails(self):
        (self.bin / "docker").write_text((self.bin / "docker").read_text().replace(
            "  inspect*) echo healthy;;", "  inspect*) echo unhealthy;;"))
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LOG=str(self.log), IMAGES="catalytix:0.8.0")
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", str(self.zip)], env=env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.removed_images(), [])
        self.assertIn("catalytix-0.8.0.zip", self.zips(self.inst))




@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class HardeningTests(CleanupTests):
    """Sicherheitsprüfung 2.15: Prüfsumme, Dateirechte aus dem Paket, .env nur für den Besitzer."""

    def rezip(self, mode=None):
        src = self.t / "src" / "catalytix"
        with zipfile.ZipFile(self.zip, "w") as z:
            for f in src.rglob("*"):
                info = zipfile.ZipInfo.from_file(f, f.relative_to(src.parent))
                if mode is not None:
                    info.external_attr = ((0o40000 if f.is_dir() else 0o100000) | mode) << 16
                z.writestr(info, b"" if f.is_dir() else f.read_bytes())

    def test_checksum_ok(self):
        import hashlib
        (self.pkgdir / "catalytix-2.0.0.zip.sha256").write_text(
            hashlib.sha256(self.zip.read_bytes()).hexdigest() + "  catalytix-2.0.0.zip\n")
        self.assertIn("Checksum matches", self.run_update("--no-cleanup"))

    def test_checksum_mismatch_changes_nothing(self):
        (self.pkgdir / "catalytix-2.0.0.zip.sha256").write_text("0" * 64 + "  catalytix-2.0.0.zip\n")
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LOG=str(self.log), IMAGES="")
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", str(self.zip)], env=env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Checksum does NOT match", r.stdout + r.stderr)
        self.assertIn("1.0.0", (self.inst / "catalytix" / "__init__.py").read_text())

    def test_checksum_missing_shows_sum(self):
        import hashlib
        out = self.run_update("--no-cleanup")
        self.assertIn("No checksum file", out)
        self.assertIn(hashlib.sha256(self.zip.read_bytes()).hexdigest(), out)

    def test_world_writable_package_files_fixed(self):
        self.rezip(0o777)
        self.run_update("--no-cleanup")
        for p in [self.inst / "Dockerfile", self.inst / "catalytix", self.inst / "catalytix" / "__init__.py"]:
            self.assertEqual(os.stat(p).st_mode & 0o022, 0, p)

    def test_new_env_private(self):
        (self.inst / ".env").unlink()
        self.run_update("--no-cleanup")
        self.assertEqual(os.stat(self.inst / ".env").st_mode & 0o077, 0)


for _name in dir(CleanupTests):
    if _name.startswith("test_") and _name not in HardeningTests.__dict__:
        setattr(HardeningTests, _name, None)


if __name__ == "__main__":
    unittest.main()
