"""update.sh ohne Compose-Mindestversion: altes docker-compose, fehlende .env, kein unzip."""
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("bash", "sh", "env", "dirname", "readlink", "mktemp", "mkdir", "rm", "mv", "cp", "find", "sed", "head",
         "tail", "sort", "printf", "cat", "chmod", "seq", "sleep", "hostname", "python3", "grep", "tr", "ls")


@unittest.skipUnless(shutil.which("bash") and shutil.which("python3"), "braucht bash und python3")
class ComposeCompatTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.log = self.t / "log"
        # „docker compose“ gibt es nicht – nur das alte eigenständige docker-compose
        (self.bin / "docker").write_text(
            '#!/bin/bash\necho "docker $*" >> "$LOG"\n'
            'case "$*" in\n  "compose version") exit 1;;\n  *com.docker.compose.project*) echo "";;\n'
            '  "inspect -f {{.State.Status}} abc") echo running;;\n  inspect*) echo healthy;;\nesac\nexit 0\n')
        (self.bin / "docker-compose").write_text(
            '#!/bin/bash\necho "docker-compose $*" >> "$LOG"\n'
            '[ -f .env ] || { echo "env file .env not found" >&2; exit 1; }\n'
            'case "$*" in "ps -q catalytix") echo abc;; esac\nexit 0\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)
        # Werkzeuge ohne unzip
        self.nounzip = self.t / "nounzip"
        self.nounzip.mkdir()
        for tool in TOOLS:
            p = shutil.which(tool)
            if p:
                (self.nounzip / tool).symlink_to(p)
        self.inst = self.t / "inst"
        self._tree(self.inst, "1.0.0")
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        pkg = self.t / "pkg" / "catalytix"
        self._tree(pkg, "2.0.0")
        shutil.copy(ROOT / "update.sh", pkg / "update.sh")
        (pkg / ".env.example").write_text("#TZ=Europe/Berlin\n")
        self.zip = self.t / "catalytix-2.0.0.zip"
        with zipfile.ZipFile(self.zip, "w") as z:
            for f in pkg.rglob("*"):
                z.write(f, f.relative_to(pkg.parent))

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    @staticmethod
    def _tree(d, version):
        (d / "catalytix").mkdir(parents=True)
        (d / "catalytix" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        for f in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (d / f).write_text("x\n")

    def run_update(self, path, zip_path=None):
        env = {"PATH": path, "LOG": str(self.log), "HOME": str(self.t)}
        return subprocess.run(["bash", str(self.inst / "update.sh"), "-y", str(zip_path or self.zip)],
                              env=env, capture_output=True, text=True)

    def test_old_compose_without_env_and_without_unzip(self):
        r = self.run_update(f"{self.bin}:{self.nounzip}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2.0.0", (self.inst / "catalytix" / "__init__.py").read_text())
        self.assertTrue((self.inst / ".env").exists())
        log = self.log.read_text()
        self.assertIn("docker-compose exec -T catalytix docker-entrypoint.sh flask --app wsgi backup", log)
        self.assertIn("docker-compose up -d --remove-orphans catalytix", log)
        self.assertNotIn("--status", log)

    def test_corrupt_zip_rejected_without_unzip(self):
        data = bytearray(self.zip.read_bytes())
        i = data.find(b'__version__ = "2.0.0"')
        data[i:i + 3] = b"XXX"                     # Inhalt kaputt, Verzeichnis intakt → nur CRC-Prüfung merkt es
        bad = self.t / "kaputt.zip"
        bad.write_bytes(bytes(data))
        r = self.run_update(f"{self.bin}:{self.nounzip}", bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("damaged", r.stdout + r.stderr)
        self.assertIn("1.0.0", (self.inst / "catalytix" / "__init__.py").read_text())

    def test_fresh_install_env_from_template(self):
        shutil.rmtree(self.inst)
        self.inst.mkdir()
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        r = self.run_update(f"{self.bin}:{self.nounzip}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.inst / ".env").read_text(), "#TZ=Europe/Berlin\n")

    def test_compose_file_has_no_new_syntax(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("required:", text)
        self.assertIn("env_file: .env", text)


if __name__ == "__main__":
    unittest.main()
