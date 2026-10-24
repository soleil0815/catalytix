"""update.sh --pull: Image von GitHub (ghcr.io) statt lokal bauen – und zurück zum klassischen Update."""
import os
import shutil
import subprocess
import tempfile
import unittest
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
IMG = "ghcr.io/soleil0815/catalytix"

DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$LOG"
case "$1" in
  compose)
    case "$*" in
      "compose version") exit 0;;
      "compose ps -q catalytix") echo abc;;
    esac;;
  inspect)
    case "$*" in
      *com.docker.compose.project*) echo "";;
      "inspect -f {{.Image}} catalytix") [ -n "$RUN_VERSION" ] && echo sha256:run;;
      "inspect -f {{.State.Status}} abc") echo running;;
      *) echo healthy;;
    esac;;
  pull) [ -n "$PULL_FAIL" ] && exit 1; exit 0;;
  image)
    case "$2" in
      inspect)
        case "$*" in
          *image.version*sha256:run*) echo "$RUN_VERSION";;
          *image.version*) echo "$IMG_VERSION";;
          *{{.Id}}*) echo sha256:prev;;
          *RepoDigests*) echo "ghcr.io/soleil0815/catalytix@sha256:abc";;
        esac;;
      ls) [ "$5" = "ghcr.io/soleil0815/catalytix" ] && printf "%s\n" $GHCR_IMAGES;;
    esac;;
  create) echo cid123;;
  cp) dest="${@: -1}"; [ -n "$NO_DIST" ] || cp -a "$DIST/." "$dest";;
esac
exit 0
'''


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class PullTests(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.bin = self.t / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.log = self.t / "docker.log"
        # klassische Installation 1.0.0
        self.inst = self.t / "inst"
        (self.inst / "catalytix").mkdir(parents=True)
        (self.inst / "catalytix" / "__init__.py").write_text('__version__ = "1.0.0"\n')
        for f in ("Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (self.inst / f).write_text("x\n")
        (self.inst / "docker-compose.yml").write_text("alt\n")
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        (self.inst / ".env").write_text("TZ=Europe/Berlin\n#CATALYTIX_PORT=9300\n")
        (self.inst / "data").mkdir()
        (self.inst / "data" / "catalytix.db").write_text("db")
        # Installationsdateien im Image (/app/dist)
        self.dist = self.t / "dist"
        self.dist.mkdir()
        shutil.copy(ROOT / "update.sh", self.dist / "update.sh")
        (self.dist / "docker-compose.yml").write_text("neu aus dem Image\n")
        (self.dist / ".env.example").write_text("#TZ=Europe/Berlin\n")
        (self.dist / "README.md").write_text("liesmich\n")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def run_update(self, *args, ok=True, **env):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", LOG=str(self.log), DIST=str(self.dist),
                 IMG_VERSION="2.0.0", GHCR_IMAGES="")
        e.update(env)
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", *args], env=e, capture_output=True,
                           text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def docker_log(self):
        return self.log.read_text() if self.log.exists() else ""

    def env(self):
        return (self.inst / ".env").read_text()

    def test_pull_uses_github_image_and_remembers_it(self):
        out = self.run_update("--pull")
        log = self.docker_log()
        self.assertIn(f"docker pull {IMG}:latest", log)
        self.assertIn(f"docker tag {IMG}:latest {IMG}:2.0.0", log)
        self.assertNotIn("compose build", log)
        self.assertIn("compose up -d --remove-orphans --no-build catalytix", log)
        self.assertIn("exec -T catalytix docker-entrypoint.sh flask --app wsgi backup", log)   # Sicherung vorher
        env = self.env()
        self.assertIn(f"CATALYTIX_IMAGE={IMG}\n", env)
        self.assertIn("CATALYTIX_TAG=latest\n", env)                                  # folgt latest
        self.assertIn("TZ=Europe/Berlin", env)                                       # eigene Werte bleiben
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), "neu aus dem Image\n")
        self.assertEqual((self.inst / "data" / "catalytix.db").read_text(), "db")
        self.assertIn("1.0.0 → 2.0.0", out)

    def test_installed_version_comes_from_running_container(self):
        out = self.run_update("--pull", RUN_VERSION="1.5.0")
        self.assertIn("1.5.0 → 2.0.0", out)

    def test_stale_program_files_removed_in_pull_mode(self):
        self.run_update("--pull")
        for name in ("catalytix", "Dockerfile", "wsgi.py", "docker-entrypoint.sh", "requirements.txt"):
            self.assertFalse((self.inst / name).exists(), name)
        self.assertTrue((self.inst / "data" / "catalytix.db").exists())

    def test_abort_after_pull_puts_tag_back(self):
        self.run_update("--pull", ok=False, NO_DIST="1")
        self.assertIn(f"docker tag sha256:prev {IMG}:latest", self.docker_log())
        self.assertNotIn("compose up", self.docker_log())

    def test_successful_pull_keeps_new_tag(self):
        self.run_update("--pull")
        self.assertNotIn("docker tag sha256:prev", self.docker_log())

    def test_old_exact_tag_follows_latest(self):
        # vor 2.17.1 schrieb --pull immer die genaue Version – das gilt nicht als absichtlich festgehalten
        (self.inst / ".env").write_text(f"CATALYTIX_IMAGE={IMG}\nCATALYTIX_TAG=1.0.0\n")
        out = self.run_update()
        self.assertIn(f"docker pull {IMG}:latest", self.docker_log())
        self.assertIn("CATALYTIX_TAG=latest\n", self.env())
        self.assertIn("Following the newest version", out)

    def test_env_from_template_replaced_in_place(self):
        shutil.copy(ROOT / ".env.example", self.inst / ".env")
        self.run_update("--pull")
        env = self.env()
        self.assertEqual(env.count("CATALYTIX_IMAGE="), 1)
        self.assertIn(f"\nCATALYTIX_IMAGE={IMG}\n", env)
        self.assertNotIn("Set by update.sh", env)

    def test_fresh_install_with_only_update_sh(self):
        shutil.rmtree(self.inst)
        self.inst.mkdir()
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        self.run_update("--pull")
        self.assertTrue((self.inst / "docker-compose.yml").exists())
        self.assertEqual((self.inst / ".env").read_text().count("CATALYTIX_TAG=latest"), 1)
        self.assertEqual(os.stat(self.inst / ".env").st_mode & 0o077, 0)

    def test_pull_exact_version_pins_until_next_pull(self):
        out = self.run_update("--pull=2.0.0")
        self.assertIn(f"docker pull {IMG}:2.0.0", self.docker_log())
        self.assertNotIn("docker tag", self.docker_log())
        self.assertIn("CATALYTIX_TAG=2.0.0\n", self.env())
        self.assertIn("CATALYTIX_PINNED=1\n", self.env())
        self.assertIn("Pinned to version 2.0.0", out)
        self.log.unlink()
        self.run_update()                                                   # bleibt bei der festen Version
        self.assertIn(f"docker pull {IMG}:2.0.0", self.docker_log())
        self.log.unlink()
        self.run_update("--pull")                                           # zurück zu latest
        self.assertIn(f"docker pull {IMG}:latest", self.docker_log())
        self.assertIn("CATALYTIX_TAG=latest\n", self.env())
        self.assertNotIn("\nCATALYTIX_PINNED=1", self.env())

    def test_plain_update_in_pull_mode_pulls_latest(self):
        self.run_update("--pull")
        self.log.unlink()
        out = self.run_update(IMG_VERSION="2.1.0")
        log = self.docker_log()
        self.assertIn(f"docker pull {IMG}:latest", log)
        self.assertNotIn("compose build", log)
        self.assertIn("CATALYTIX_TAG=latest\n", self.env())
        self.assertIn("→ 2.1.0", out)

    def test_zip_switches_back_to_local_build(self):
        self.run_update("--pull")
        pkg = self.t / "pkg" / "catalytix"
        (pkg / "catalytix").mkdir(parents=True)
        (pkg / "catalytix" / "__init__.py").write_text('__version__ = "3.0.0"\n')
        for f in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (pkg / f).write_text("x\n")
        shutil.copy(ROOT / "update.sh", pkg / "update.sh")
        z = self.t / "catalytix-3.0.0.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for f in pkg.rglob("*"):
                zf.write(f, f.relative_to(pkg.parent))
        self.log.unlink()
        out = self.run_update(str(z))
        self.assertIn("compose build", self.docker_log())
        self.assertIn(f"#CATALYTIX_IMAGE={IMG}", self.env())
        self.assertIn("building locally", out)

    def test_pull_failure_changes_nothing(self):
        out = self.run_update("--pull", ok=False, PULL_FAIL="1")
        self.assertIn("Could not pull", out)
        self.assertNotIn("CATALYTIX_IMAGE", self.env())
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), "alt\n")

    def test_image_without_installation_files(self):
        out = self.run_update("--pull", ok=False, NO_DIST="1")
        self.assertIn("installation files", out)
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), "alt\n")

    def test_invalid_arguments(self):
        self.assertIn("expects a version", self.run_update("--pull=foo;rm", ok=False))
        self.assertIn("not both", self.run_update("--pull", str(self.t / "x.zip"), ok=False))

    def test_cleanup_old_github_images(self):
        self.run_update("--pull", GHCR_IMAGES=f"{IMG}:0.9.0 {IMG}:1.5.0 {IMG}:2.0.0 {IMG}:latest")
        removed = [l.split()[-1] for l in self.docker_log().splitlines() if l.startswith("docker image rm ")]
        self.assertEqual(removed, [f"{IMG}:0.9.0", f"{IMG}:1.5.0"])


class RepoFilesTests(unittest.TestCase):
    """Für GitHub: Versionen stimmen überein, Workflow vorhanden, nichts von Dockhand."""

    def test_versions_match(self):
        import catalytix
        v = catalytix.__version__
        self.assertIn(f'org.opencontainers.image.version="{v}"', (ROOT / "Dockerfile").read_text())
        self.assertIn(f"CATALYTIX_TAG:-{v}}}", (ROOT / "docker-compose.yml").read_text())

    def test_workflow_builds_and_pushes_image(self):
        # ohne PyYAML: das ist keine Abhängigkeit von Catalytix und fehlt deshalb auf GitHub
        text = (ROOT / ".github" / "workflows" / "build.yml").read_text()
        for job in ("  test:", "  image:", "  release:"):
            self.assertIn(f"\n{job}\n", text)
        self.assertIn("ghcr.io/${GITHUB_REPOSITORY,,}", text)
        self.assertIn("linux/amd64,linux/arm64", text)

    def test_trivy_scan_before_publishing(self):
        import re
        for name in ("build.yml", "rebuild.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text()
            uses = re.findall(r"aquasecurity/trivy-action@(\S+)", text)
            self.assertTrue(uses, name)
            for ref in uses:      # nach dem Supply-Chain-Angriff 2026: nur auf einen vollen Commit gepinnt
                self.assertRegex(ref, r"^[0-9a-f]{40}$", name)
            self.assertIn('exit-code: "1"', text)
            self.assertIn("severity: HIGH,CRITICAL", text)
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text()
        self.assertLess(build.index("trivy-action"), build.index("name: Build (and push)"))
        weekly = (ROOT / ".github" / "workflows" / "rebuild.yml").read_text()
        self.assertIn("ref: ${{ needs.release.outputs.tag }}", weekly)       # nur veröffentlichte Releases
        self.assertIn("needs: [release, test]", weekly)                        # erst nach den Tests
        self.assertLess(weekly.index("trivy-action"), weekly.index("name: Build and publish"))
        self.assertIn("publish-refs/heads/main", weekly)                       # nie gleichzeitig mit build.yml
        self.assertIn("group: publish-${{ github.ref }}", build)
        self.assertTrue((ROOT / ".trivyignore").exists())

    def test_image_carries_installation_files(self):
        df = (ROOT / "Dockerfile").read_text()
        self.assertIn("/app/dist/", df)
        self.assertNotIn("update.sh", (ROOT / ".dockerignore").read_text())

    def test_no_dockhand(self):
        for p in ("docker-compose.yml", "Dockerfile", "update.sh", "README.md", "README.en.md"):
            self.assertNotIn("dockhand", (ROOT / p).read_text().lower(), p)

    def test_gitignore_keeps_secrets_out(self):
        gi = (ROOT / ".gitignore").read_text()
        for entry in ("data/", "instance/", ".env", "secret_key", "*.db", "*.zip"):
            self.assertIn(entry, gi)


if __name__ == "__main__":
    unittest.main()
