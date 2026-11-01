#!/usr/bin/env python3
"""Security scan of a container image with Trivy – without the GitHub actions trivy-action/setup-trivy.

  python3 tools/trivy_scan.py install            download the release from tools/trivy.lock, check its SHA-256,
                                                 unpack to ./.trivy/trivy (refuses on a checksum mismatch)
  python3 tools/trivy_scan.py check-ignore       validate .trivyignore (expiry date and reason for every entry)
  python3 tools/trivy_scan.py scan IMAGE         scan; exit code 1 if a HIGH or CRITICAL vulnerability with an
                                                 available fix is found (or a secret). Unfixed findings don't block.

The result is shown readably on the GitHub run: a table in the job summary and one annotation with the list.
TRIVY_BIN overrides the binary (used by the self-tests with a stand-in Trivy).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "tools" / "trivy.lock"
IGNORE = ROOT / ".trivyignore"
INSTALL_DIR = ROOT / ".trivy"
MAX_IGNORE_DAYS = 183


def annotate(level: str, title: str, message: str) -> None:
    """GitHub annotation (visible on the run without opening the log); newlines encoded as %0A."""
    msg = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::{level} title={title}::{msg}")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def read_lock(path: Path = LOCK) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    for key in ("version", "url", "sha256"):
        if not values.get(key):
            raise SystemExit(f"trivy.lock: {key} missing")
    if not re.fullmatch(r"[0-9a-f]{64}", values["sha256"]):
        raise SystemExit("trivy.lock: sha256 must be 64 hex characters")
    if not values["url"].startswith("https://github.com/aquasecurity/trivy/releases/download/"):
        raise SystemExit("trivy.lock: url must be an official Trivy release on github.com")
    return values


def install(lock: dict[str, str] | None = None, fetch: Callable[[str, Path], object] | None = None) -> Path:
    lock = lock or read_lock()
    INSTALL_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "trivy.tar.gz"
        if fetch:
            fetch(lock["url"], archive)
        else:
            with urllib.request.urlopen(lock["url"], timeout=120) as res, open(archive, "wb") as out:  # noqa: S310
                out.write(res.read())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != lock["sha256"]:
            annotate("error", "Trivy download rejected",
                     f"SHA-256 of {lock['url']} is {digest}, expected {lock['sha256']} – not running it.")
            raise SystemExit(1)
        with tarfile.open(archive) as tar:
            member = tar.getmember("trivy")
            if not member.isfile():
                raise SystemExit("trivy archive: 'trivy' is not a regular file")
            tar.extract(member, INSTALL_DIR, filter="data")
    binary = INSTALL_DIR / "trivy"
    binary.chmod(0o755)
    print(f"Trivy {lock['version']} installed, SHA-256 verified: {binary}")
    return binary


def check_ignore(path: Path = IGNORE, today: dt.date | None = None) -> list[str]:
    """Every entry: ID exp:YYYY-MM-DD (not more than MAX_IGNORE_DAYS ahead) and a comment line above as reason."""
    today = today or dt.date.today()
    problems, prev_comment = [], ""
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            prev_comment = ""
            continue
        if line.startswith("#"):
            prev_comment = line.lstrip("#").strip()
            continue
        m = re.fullmatch(r"(\S+)\s+exp:(\d{4}-\d{2}-\d{2})", line)
        if not m:
            problems.append(f".trivyignore line {n}: '{line}' needs the form 'ID exp:YYYY-MM-DD'")
        else:
            exp = dt.date.fromisoformat(m.group(2))
            if (exp - today).days > MAX_IGNORE_DAYS:
                problems.append(f".trivyignore line {n}: expiry {exp} is more than 6 months ahead")
            if exp < today:
                problems.append(f".trivyignore line {n}: {m.group(1)} expired on {exp} – check again or remove it")
        if not prev_comment:
            problems.append(f".trivyignore line {n}: reason missing (comment line directly above)")
        prev_comment = ""
    return problems


Finding = tuple[str, str, str, str, str, str, str]   # severity, id, package, installed, fixed, path, title


def findings(report: dict[str, Any]) -> list[Finding]:
    rows: list[Finding] = []
    for res in report.get("Results") or []:
        for v in res.get("Vulnerabilities") or []:
            if v.get("Severity") in ("HIGH", "CRITICAL") and v.get("FixedVersion"):
                rows.append((v.get("Severity", ""), v.get("VulnerabilityID", ""), v.get("PkgName", ""),
                             v.get("InstalledVersion", ""), v.get("FixedVersion", ""),
                             v.get("PkgPath") or res.get("Target", ""), (v.get("Title") or "")[:90]))
        for s in res.get("Secrets") or []:
            rows.append((s.get("Severity", ""), "secret:" + s.get("RuleID", ""), res.get("Target", ""),
                         "", "", s.get("Match", "")[:40], (s.get("Title") or "")[:90]))
    return rows


def scan(image: str, binary: str | None = None, out: Path = Path("trivy.json")) -> int:
    problems = check_ignore()
    if problems:
        annotate("error", "Invalid .trivyignore", "\n".join(problems))
        return 1
    binary = binary or os.environ.get("TRIVY_BIN") or str(INSTALL_DIR / "trivy")
    cmd = [binary, "image", "--quiet", "--scanners", "vuln,secret", "--severity", "HIGH,CRITICAL",
           "--ignore-unfixed", "--ignorefile", str(IGNORE), "--format", "json", "--output", str(out),
           "--exit-code", "0", image]
    res = subprocess.run(cmd)
    if res.returncode != 0 or not out.exists():
        annotate("error", "Trivy could not scan", f"Trivy exited with {res.returncode} for {image} (see log).")
        return 1
    rows = findings(json.loads(out.read_text(encoding="utf-8")))
    summary(f"### Trivy: {len(rows)} blocking finding(s) in `{image}`\n"
            "HIGH/CRITICAL with an available fix (unfixed findings and accepted entries in .trivyignore excluded)\n")
    if not rows:
        print(f"Trivy: no blocking findings in {image}")
        return 0
    summary("| Severity | ID | Package | Installed | Fixed in | Where | Title |\n|---|---|---|---|---|---|---|")
    for r in rows:
        summary("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
    annotate("error", f"Trivy: {len(rows)} blocking finding(s) – nothing is published",
             "\n".join(f"{r[0]} {r[1]} {r[2]} {r[3]} -> {r[4]}" for r in rows))
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "install":
        install()
        return 0
    if argv[0] == "check-ignore":
        problems = check_ignore()
        if problems:
            annotate("error", "Invalid .trivyignore", "\n".join(problems))
        return 1 if problems else 0
    if argv[0] == "scan" and len(argv) == 2:
        return scan(argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
