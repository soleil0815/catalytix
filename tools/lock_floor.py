"""Build step for DEPS=current: reviewed versions from requirements.txt are a lower bound.

The fresh resolve only takes releases older than DEPS_CUTOFF (cool-down against malicious uploads). A security fix
that was reviewed and pinned in requirements.txt on purpose (e.g. after a Trivy finding) may be younger than that –
it must not be held back. This script installs every pinned version that is newer than the installed one (without
dependencies, exactly as pinned) and prints what it raised. Uses the packaging copy vendored in pip (pip is removed
from the image afterwards).
"""
import importlib.metadata as md
import subprocess
import sys

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version


def main(lock: str) -> int:
    raise_to = []
    for line in open(lock, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate():
            continue
        pinned = [s.version for s in req.specifier if s.operator == "=="]
        if not pinned:
            continue
        try:
            installed = Version(md.version(req.name))
        except md.PackageNotFoundError:
            continue            # not part of the fresh resolve (any more) – nothing to raise
        if Version(pinned[0]) > installed:
            raise_to.append(f"{canonicalize_name(req.name)}=={pinned[0]}")
            print(f"lock floor: {req.name} {installed} -> {pinned[0]} (reviewed pin, newer than the cool-down)")
    if raise_to:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-deps", "--only-binary=:all:", *raise_to])
    else:
        print("lock floor: nothing to raise")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"))
