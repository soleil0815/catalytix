#!/usr/bin/env python3
"""Helpers for the weekly security update (.github/workflows/security-update.yml).

  diff OLD NEW                 compare two package lists (/app/image-packages.txt of the published and the new
                               image); prints the changes, exit code 0 = changed, 3 = unchanged
  next-tag VERSION             next security tag for VERSION from CHANGELOG.md, e.g. 2.18.3-s2 (counter starts at
                               s1 for every program version)
  changelog TAG VERSION OLD NEW  insert the change log entry for TAG (list of changed packages) into CHANGELOG.md
  is-highest VERSION LATEST    exit code 0 if VERSION >= LATEST (only then may :latest be set)

Package list format (one per line): "base <image>", "python <version>", "deb <name> <version>", "pip <name> <version>".
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
UNCHANGED = 3
KINDS = {"base": "Basis-Image", "python": "Python", "deb": "Debian-Pakete", "pip": "Python-Pakete"}


def parse(text: str) -> dict[tuple[str, str], str]:
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] in ("base", "python") and len(parts) == 2:
            out[(parts[0], parts[0])] = parts[1]
        elif parts[0] in ("deb", "pip") and len(parts) == 3:
            out[(parts[0], parts[1])] = parts[2]
    return out


def diff(old: str, new: str) -> list[tuple[str, str, str, str]]:
    """[(kind, name, old version or '', new version or '')] – sorted by kind and name."""
    a, b = parse(old), parse(new)
    changes = []
    for key in sorted(set(a) | set(b), key=lambda k: (list(KINDS).index(k[0]), k[1])):
        if a.get(key) != b.get(key):
            changes.append((key[0], key[1], a.get(key, ""), b.get(key, "")))
    return changes


def format_changes(changes) -> list[str]:
    lines = []
    for kind in KINDS:
        items = [c for c in changes if c[0] == kind]
        if not items:
            continue
        parts = []
        for _, name, old, new in items:
            label = "" if kind in ("base", "python") else f"{name} "
            if old and new:
                parts.append(f"{label}{old} → {new}")
            elif new:
                parts.append(f"{label}neu {new}")
            else:
                parts.append(f"{label}entfernt ({old})")
        lines.append(f"- {KINDS[kind]}: " + ", ".join(f"`{p}`" for p in parts))
    return lines


def next_tag(version: str, text: str | None = None) -> str:
    text = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    used = [int(n) for n in re.findall(rf"^## {re.escape(version)}-s(\d+) ", text, re.M)]
    return f"{version}-s{max(used, default=0) + 1}"


def changelog_entry(tag: str, version: str, changes, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return "\n".join([
        f"## {tag} – {today:%d.%m.%Y} (Sicherheitsupdate)",
        f"Gleiches Programm wie {version}, nur aktualisierte Pakete (wöchentliches Sicherheitsupdate, automatisch "
        "gebaut, getestet und mit Trivy geprüft):",
        *format_changes(changes),
        "",
    ]) + "\n"


def insert_changelog(entry: str, text: str) -> str:
    i = text.find("\n## ")
    if i == -1:
        return text.rstrip("\n") + "\n\n" + entry
    return text[:i + 1] + entry + text[i + 1:]


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.match(r"^v?(\d+(?:\.\d+)*)", v).group(1).split("."))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd == "diff" and len(argv) == 3:
        changes = diff(Path(argv[1]).read_text(), Path(argv[2]).read_text())
        for line in format_changes(changes):
            print(line)
        return 0 if changes else UNCHANGED
    if cmd == "next-tag" and len(argv) == 2:
        print(next_tag(argv[1]))
        return 0
    if cmd == "changelog" and len(argv) == 5:
        tag, version, old, new = argv[1:]
        changes = diff(Path(old).read_text(), Path(new).read_text())
        if not changes:
            print("no changes – no change log entry")
            return UNCHANGED
        CHANGELOG.write_text(insert_changelog(changelog_entry(tag, version, changes),
                                              CHANGELOG.read_text(encoding="utf-8")), encoding="utf-8")
        return 0
    if cmd == "is-highest" and len(argv) == 3:
        return 0 if vtuple(argv[1]) >= vtuple(argv[2]) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
