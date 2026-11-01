#!/usr/bin/env python3
"""Fix all versions of the image anew – always INSIDE the foundation, never across it.

What is locked (and where):
- Base image:      Dockerfile  ARG PYTHON_IMAGE=python:<3.13.x>-slim-trixie@sha256:<digest>
- Debian packages: Dockerfile  ARG DEBIAN_SNAPSHOT=<timestamp>  (exact archive state on snapshot.debian.org)
- Python packages: requirements.txt  (all, including indirect ones, exact version + SHA-256 of every file)
- pip itself:      docker/pip.lock   (only used while building; removed from the finished image)

Rules ("foundation" = FOUNDATION below; raising it is a deliberate change, never done by this script):
- Base image: same Debian release (codename in the tag) and same Python series (e.g. 3.13).
- Python packages: newest version within the same major version (before 1.0: same minor version), and within the
  ranges of requirements.in. Calendar versions (e.g. certifi 2026.10.19, tzdata) have no upper limit.
- Only releases at least COOLDOWN_DAYS old (protection against freshly uploaded malicious releases); a version that is
  already locked is always allowed. 0 switches the cool-down off (e.g. for an urgent security fix).

Usage:  python3 tools/lock.py update [--cooldown-days 7] [--report changes.json]
        python3 tools/lock.py check          # only verify the locks against the rules (used by the tests)

`update` needs Docker (the dependencies are resolved inside the new base image, i.e. for exactly that Python and
platform) and network access to Docker Hub and PyPI. Changes are written in place; the report lists old -> new.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_IN = ROOT / "requirements.in"
PIP_LOCK = ROOT / "docker" / "pip.lock"

FOUNDATION = {"python": "3.13", "debian": "trixie", "variant": "slim"}
REGISTRY = "https://registry-1.docker.io"
PYPI = "https://pypi.org/pypi"
COOLDOWN_DAYS = 7

PYTHON_IMAGE_RE = re.compile(r"^ARG PYTHON_IMAGE=(python:(\d+\.\d+\.\d+)-(\w+)-(\w+)@sha256:([0-9a-f]{64}))$", re.M)
SNAPSHOT_RE = re.compile(r"^ARG DEBIAN_SNAPSHOT=(\d{8}T\d{6}Z)$", re.M)
LOCK_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(;[^\\#]*)?")


# ------------------------------------------------------------------ versions and limits

def parse_version(v: str) -> tuple[int, ...]:
    nums = re.match(r"^(\d+(?:\.\d+)*)", v)
    if not nums:
        raise ValueError(f"unsupported version {v!r}")
    return tuple(int(x) for x in nums.group(1).split("."))


# Calendar-versioned packages whose first number is a short year (pip 26.x, packaging 26.x) – like certifi 2026.10.19
# they make no compatibility promise with the number, so they have no upper limit.
CALENDAR_PACKAGES = {"pip", "packaging", "certifi", "tzdata"}


def is_calendar_version(v: str, name: str = "") -> bool:
    """certifi 2026.10.19, tzdata 2026.2, pip 26.2 … – always take the newest."""
    return parse_version(v)[0] >= 2000 or name.lower() in CALENDAR_PACKAGES


def upper_bound(v: str, name: str = "") -> tuple[int, ...] | None:
    """Exclusive upper limit within the foundation: next major version; before 1.0 the next minor version."""
    if is_calendar_version(v, name):
        return None
    parts = parse_version(v) + (0, 0)
    major, minor = parts[0], parts[1]
    return (0, minor + 1) if major == 0 else (major + 1,)


def within(candidate: str, current: str, name: str = "") -> bool:
    """May `candidate` replace `current`? Never lower, never across the limit."""
    c, cur = parse_version(candidate), parse_version(current)
    if c < cur:
        return False
    limit = upper_bound(current, name)
    return limit is None or c[:len(limit)] < limit


def bound_spec(name: str, current: str) -> str:
    limit = upper_bound(current, name)
    spec = f"{name}>={current}"
    return spec if limit is None else spec + ",<" + ".".join(str(x) for x in limit)


def is_prerelease(v: str) -> bool:
    """Only final releases (1.2.3, 1.2.3.post1) are taken – no alpha/beta/rc/dev versions."""
    return not re.fullmatch(r"\d+(\.\d+)*(\.post\d+)?", v)


# ------------------------------------------------------------------ lock files

class LockEntry(TypedDict):
    name: str
    version: str
    marker: str
    hashes: list[str]


def read_lock(path: Path = REQUIREMENTS) -> dict[str, LockEntry]:
    """name (lower case) -> {"name", "version", "marker", "hashes"}"""
    pkgs: dict[str, LockEntry] = {}
    current: LockEntry | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        m = LOCK_LINE_RE.match(line.strip())
        if m:
            current = {"name": m.group(1), "version": m.group(2),
                       "marker": (m.group(3) or "").lstrip(";").strip(), "hashes": []}
            pkgs[m.group(1).lower().replace("_", "-")] = current
            line = line[m.end():]
        for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if current is not None:
                current["hashes"].append(h)
    return pkgs


PIP_HEADER = [
    "# pip for building the image only (removed from the finished image) – written by tools/lock.py.",
    "# Exact version and SHA-256 of every published file (calendar version – no upper limit).",
    "",
]


def write_lock(pkgs: dict[str, LockEntry], path: Path = REQUIREMENTS) -> None:
    lines = list(PIP_HEADER) if path == PIP_LOCK else [
        "# ------------------------------------------------------------------",
        "# Exactly pinned package versions (lock file) – written by tools/lock.py, do not edit by hand.",
        "#",
        "# Every package, including indirect dependencies, with exact version and the SHA-256 of every",
        "# published file: pip refuses anything else (hash checking). Direct dependencies and their",
        "# allowed ranges are in requirements.in.",
        "#",
        "# Installation:   pip install --require-hashes --no-deps -r requirements.txt",
        "# New versions:    python3 tools/lock.py update   (within the foundation, see the script)",
        "# ------------------------------------------------------------------",
        "",
    ]
    for key in sorted(pkgs):
        p = pkgs[key]
        head = f"{p['name']}=={p['version']}"
        if p.get("marker"):
            head += f" ; {p['marker']}"
        hashes = sorted(set(p["hashes"]))
        if not hashes:
            raise SystemExit(f"lock: no hashes for {p['name']} {p['version']}")
        lines.append(head + " \\")
        lines += [f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else "") for i, h in enumerate(hashes)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_dockerfile(text: str | None = None) -> dict[str, str]:
    text = text if text is not None else DOCKERFILE.read_text(encoding="utf-8")
    m = PYTHON_IMAGE_RE.search(text)
    s = SNAPSHOT_RE.search(text)
    if not m or not s:
        raise SystemExit("lock: Dockerfile has no locked ARG PYTHON_IMAGE / ARG DEBIAN_SNAPSHOT line")
    return {"image": m.group(1), "python": m.group(2), "variant": m.group(3), "debian": m.group(4),
            "digest": m.group(5), "snapshot": s.group(1)}


def write_dockerfile(image: str, snapshot: str) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    text = PYTHON_IMAGE_RE.sub(f"ARG PYTHON_IMAGE={image}", text, count=1)
    text = SNAPSHOT_RE.sub(f"ARG DEBIAN_SNAPSHOT={snapshot}", text, count=1)
    DOCKERFILE.write_text(text, encoding="utf-8")


def requirements_in() -> dict[str, str]:
    """name (lower case) -> requirement line (with range and marker) from requirements.in"""
    out: dict[str, str] = {}
    for raw in REQUIREMENTS_IN.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m is None:
                raise SystemExit(f"lock: requirements.in: cannot read {line!r}")
            out[m.group(1).lower().replace("_", "-")] = line
    return out


# ------------------------------------------------------------------ network (replaceable in tests)

def http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as res:   # noqa: S310 – fixed https hosts
        return json.loads(res.read().decode()), dict(res.headers)


def pypi_release(name: str, version: str) -> Any:
    data, _ = http_json(f"{PYPI}/{name}/{version}/json")
    return data


def pypi_project(name: str) -> Any:
    data, _ = http_json(f"{PYPI}/{name}/json")
    return data


def file_hashes(name: str, version: str) -> list[str]:
    files = pypi_release(name, version).get("urls") or []
    hashes = [f["digests"]["sha256"] for f in files if not f.get("yanked")]
    if not hashes:
        raise SystemExit(f"lock: PyPI lists no files for {name} {version}")
    return hashes


def newest_on_pypi(name: str, current: str, cutoff: dt.datetime | None) -> str:
    """Newest release of `name` within the limit of `current`, uploaded before `cutoff` (wheel or sdist)."""
    best = current
    for v, files in (pypi_project(name).get("releases") or {}).items():
        if is_prerelease(v) or not files or all(f.get("yanked") for f in files):
            continue
        try:
            if not within(v, current, name) or parse_version(v) <= parse_version(best):
                continue
        except ValueError:
            continue
        uploaded = min(dt.datetime.fromisoformat(f["upload_time_iso_8601"].replace("Z", "+00:00")) for f in files)
        if cutoff is None or uploaded <= cutoff:
            best = v
    return best


def registry_token(repo: str) -> str:
    data, _ = http_json(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull")
    return str(data["token"])


MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def registry_manifest(repo: str, ref: str, token: str) -> tuple[Any, str]:
    data, headers = http_json(f"{REGISTRY}/v2/{repo}/manifests/{ref}",
                              {"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES})
    digest = next((v for k, v in headers.items() if k.lower() == "docker-content-digest"), "")
    return data, digest


def registry_blob(repo: str, digest: str, token: str) -> Any:
    data, _ = http_json(f"{REGISTRY}/v2/{repo}/blobs/{digest}", {"Authorization": f"Bearer {token}"})
    return data


def newest_base_image() -> tuple[str, str, str]:
    """(image reference with exact tag and digest, python version, debian codename) – newest within the foundation.

    The moving series tag (e.g. 3.13-slim-trixie) is only used to FIND the newest patch version; the lock always
    names the exact tag (3.13.17-slim-trixie) plus its digest."""
    repo = "library/python"
    token = registry_token(repo)
    series = f"{FOUNDATION['python']}-{FOUNDATION['variant']}-{FOUNDATION['debian']}"
    index, _ = registry_manifest(repo, series, token)
    amd64 = next((m for m in index.get("manifests", [])
                  if m.get("platform", {}).get("os") == "linux" and m["platform"].get("architecture") == "amd64"),
                 None)
    if amd64 is None:
        raise SystemExit(f"lock: {series} has no linux/amd64 image")
    manifest, _ = registry_manifest(repo, amd64["digest"], token)
    config = registry_blob(repo, manifest["config"]["digest"], token)
    env = dict(e.split("=", 1) for e in config.get("config", {}).get("Env", []) if "=" in e)
    version = env.get("PYTHON_VERSION", "")
    if not version.startswith(FOUNDATION["python"] + "."):
        raise SystemExit(f"lock: {series} carries Python {version!r} – outside the foundation {FOUNDATION['python']}")
    exact = f"{version}-{FOUNDATION['variant']}-{FOUNDATION['debian']}"
    _, digest = registry_manifest(repo, exact, token)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
        raise SystemExit(f"lock: no digest for python:{exact}")
    return f"python:{exact}@{digest}", version, FOUNDATION["debian"]


def resolve_in_image(image: str, constraints: str, pip_version: str) -> dict[str, str]:
    """Resolve requirements.in inside the (new) base image – for exactly its Python and platform.
    Returns name (lower case) -> version of everything pip would install."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "requirements.in").write_text(REQUIREMENTS_IN.read_text(encoding="utf-8"), encoding="utf-8")
        Path(tmp, "constraints.txt").write_text(constraints, encoding="utf-8")
        script = " && ".join([
            f"pip install -q --disable-pip-version-check pip=={pip_version}",
            "pip install -q --disable-pip-version-check --dry-run --ignore-installed --only-binary=:all: "
            "--report /w/report.json -r /w/requirements.in -c /w/constraints.txt",
        ])
        subprocess.run(["docker", "run", "--rm", "--user", "0", "-v", f"{tmp}:/w", "--entrypoint", "sh", image,
                        "-c", script], check=True)
        report = json.loads(Path(tmp, "report.json").read_text())
    return {i["metadata"]["name"].lower().replace("_", "-"): i["metadata"]["version"] for i in report["install"]}


# ------------------------------------------------------------------ update

def plan_python(old: dict[str, LockEntry], resolved: dict[str, str], cutoff: dt.datetime | None,
                pip_old: str) -> tuple[dict[str, LockEntry], list[str], str]:
    """New lock content from the resolver result; entries for other platforms (markers that don't apply in the image,
    e.g. waitress on Windows) are raised within their limit on their own (they have no dependencies)."""
    new: dict[str, LockEntry] = {}
    changes: list[str] = []
    for key, version in resolved.items():
        prev = old.get(key)
        if prev and not within(version, prev["version"], key):
            raise SystemExit(f"lock: {key} {prev['version']} -> {version} would leave the foundation")
        name = prev["name"] if prev else key
        new[key] = {"name": name, "version": version, "marker": prev["marker"] if prev else "", "hashes": []}
    for key, prev in old.items():
        if key in new:
            continue
        if prev["marker"]:                                  # only for another platform
            v = newest_on_pypi(prev["name"], prev["version"], cutoff)
            new[key] = {"name": prev["name"], "version": v, "marker": prev["marker"], "hashes": []}
    for key, p in new.items():
        prev = old.get(key)
        if prev is None:
            changes.append(f"pip {p['name']}: new {p['version']}")
        elif prev["version"] != p["version"]:
            changes.append(f"pip {p['name']}: {prev['version']} -> {p['version']}")
        p["hashes"] = prev["hashes"] if prev and prev["version"] == p["version"] and prev["hashes"] \
            else file_hashes(p["name"], p["version"])
    for key, prev in old.items():
        if key not in new:
            changes.append(f"pip {prev['name']}: removed ({prev['version']})")
    pip_new = newest_on_pypi("pip", pip_old, cutoff)
    return new, changes, pip_new


def constraints_for(old: dict[str, LockEntry], cutoff: dt.datetime | None,
                    newest: Callable[[str, str, dt.datetime | None], str] | None = None) -> str:
    """Limits for the resolver: every locked package stays within its foundation (never lower, never across) and
    may only move to a release older than the cool-down. The cool-down never excludes the version that is already
    locked (e.g. a security fix that was taken on purpose) – it only applies to newer ones."""
    newest = newest or newest_on_pypi
    lines = []
    for p in old.values():      # also entries with a marker (gunicorn): a constraint for an unused package is harmless
        allowed = newest(p["name"], p["version"], cutoff)
        lines.append(f"{p['name']}>={p['version']},<={allowed}")
    return "\n".join(lines) + "\n"


def update(cooldown_days: int = COOLDOWN_DAYS, report: Path | None = None, now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=cooldown_days) if cooldown_days > 0 else None
    docker = read_dockerfile()
    changes: list[str] = []

    image, version, debian = newest_base_image()
    if not within(version, docker["python"]) or debian != docker["debian"] \
            or parse_version(version)[:2] != parse_version(docker["python"])[:2]:
        raise SystemExit(f"lock: base image {image} is outside the foundation of {docker['image']}")
    if image != docker["image"]:
        changes.append(f"base {docker['image']} -> {image}")
    # Exact Debian archive state: yesterday 00:00 UTC – a moment that is completely imported on snapshot.debian.org,
    # so every build with this value (test, scan, publish) gets exactly the same packages
    snapshot = (now - dt.timedelta(days=1)).strftime("%Y%m%dT000000Z")

    old = read_lock()
    pip_lock = read_lock(PIP_LOCK)
    pip_old = pip_lock["pip"]["version"]
    resolved = resolve_in_image(image, constraints_for(old, cutoff), pip_old)
    new, py_changes, pip_new = plan_python(old, resolved, cutoff, pip_old)
    changes += py_changes
    if pip_new != pip_old:
        changes.append(f"pip pip (build only): {pip_old} -> {pip_new}")
    if pip_new != pip_old or not pip_lock["pip"]["hashes"]:
        pip_lock["pip"]["version"] = pip_new
        pip_lock["pip"]["hashes"] = file_hashes("pip", pip_new)

    write_dockerfile(image, snapshot)
    write_lock(new)
    write_lock(pip_lock, PIP_LOCK)
    if report:
        report.write_text(json.dumps({"image": image, "snapshot": snapshot, "changes": changes}, indent=1))
    for c in changes:
        print(c)
    print(f"Debian packages: archive state {snapshot}")
    return changes


# ------------------------------------------------------------------ check (no network)

def check() -> list[str]:
    """Problems with the locks (empty list = fine). Used by the self-tests and the workflows."""
    problems = []
    try:
        d = read_dockerfile()
        if d["debian"] != FOUNDATION["debian"] or not d["python"].startswith(FOUNDATION["python"] + "."):
            problems.append(f"base image {d['image']} is not in the foundation {FOUNDATION}")
    except SystemExit as exc:
        problems.append(str(exc))
    lock = read_lock()
    ranges = requirements_in()
    for key, p in lock.items():
        if not p["hashes"]:
            problems.append(f"{p['name']} has no --hash")
    for key, line in ranges.items():
        if key not in lock:
            problems.append(f"{key} from requirements.in is missing in requirements.txt")
    pip_lock = read_lock(PIP_LOCK)
    if "pip" not in pip_lock or not pip_lock["pip"]["hashes"]:
        problems.append("docker/pip.lock must pin pip with --hash")
    return problems


LOCK_FILES = ("requirements.txt", "docker/pip.lock")


def normalized_build(root: Path) -> str:
    """Everything that defines the build except the locked values – must be equal between release and main."""
    text = (root / "Dockerfile").read_text(encoding="utf-8")
    text = PYTHON_IMAGE_RE.sub("ARG PYTHON_IMAGE=<locked>", text)
    text = SNAPSHOT_RE.sub("ARG DEBIAN_SNAPSHOT=<locked>", text)
    return text + "\n---\n" + (root / "requirements.in").read_text(encoding="utf-8")


def copy_locks(src: Path, dst: Path) -> None:
    """Take over the locked values of `src` (base image, Debian snapshot, lock files) into `dst`."""
    d = read_dockerfile((src / "Dockerfile").read_text(encoding="utf-8"))
    target = (dst / "Dockerfile").read_text(encoding="utf-8")
    target = PYTHON_IMAGE_RE.sub(f"ARG PYTHON_IMAGE={d['image']}", target, count=1)
    target = SNAPSHOT_RE.sub(f"ARG DEBIAN_SNAPSHOT={d['snapshot']}", target, count=1)
    (dst / "Dockerfile").write_text(target, encoding="utf-8")
    for name in LOCK_FILES:
        (dst / name).parent.mkdir(parents=True, exist_ok=True)
        (dst / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update")
    up.add_argument("--cooldown-days", type=int, default=COOLDOWN_DAYS)
    up.add_argument("--report", type=Path)
    sub.add_parser("check")
    cp = sub.add_parser("copy-locks", help="take over the locked values from another checkout")
    cp.add_argument("src", type=Path)
    cp.add_argument("dst", type=Path)
    sb = sub.add_parser("same-build", help="exit 1 if two checkouts differ in anything but the locked values")
    sb.add_argument("a", type=Path)
    sb.add_argument("b", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "update":
        update(args.cooldown_days, args.report)
        return 0
    if args.cmd == "copy-locks":
        copy_locks(args.src, args.dst)
        return 0
    if args.cmd == "same-build":
        same = normalized_build(args.a) == normalized_build(args.b)
        if not same:
            print("::error title=Unreleased build changes on main::Dockerfile or requirements.in on main differ from "
                  "the latest release. Release them first (new program version) – the security update only renews "
                  "packages of a released program.")
        return 0 if same else 1
    problems = check()
    for p in problems:
        print("lock check:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
