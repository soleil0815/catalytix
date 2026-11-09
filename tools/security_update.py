#!/usr/bin/env python3
"""Helpers for the monthly security update on patch day (.github/workflows/security-update.yml).

  diff OLD NEW                 compare two package lists (/app/image-packages.txt of the published and the new
                               image); prints the changes, exit code 0 = changed, 3 = unchanged
  next-tag VERSION             next security tag for VERSION from CHANGELOG.md, e.g. 2.18.3-s2 (counter starts at
                               s1 for every program version)
  changelog TAG VERSION OLD NEW  insert the change log entry for TAG (list of changed packages) into CHANGELOG.md
                               (German) and CHANGELOG.en.md (English)
  is-highest VERSION LATEST    exit code 0 if VERSION >= LATEST (only then may :latest be set)
  highest                      read release tags (one per line) from stdin, print the highest version (not GitHub's
                               "latest" flag, which only marks the most recently created release)
  label KEY                    read "imagetools inspect --format '{{json .Image}}'" from stdin, print label KEY
                               (empty if missing; exit code 1 if unreadable)

Package list format (one per line): "base <image>", "python <version>", "deb <name> <version>", "pip <name> <version>".
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
CHANGELOG_EN = ROOT / "CHANGELOG.en.md"
UNCHANGED = 3
KINDS = {"base": "Basis-Image", "python": "Python", "deb": "Debian-Pakete", "pip": "Python-Pakete"}
# Texts of the change log entry per language (CHANGELOG.md is German, CHANGELOG.en.md English)
TEXTS: dict[str, dict[str, Any]] = {
    "de": {"kinds": KINDS, "new": "neu", "removed": "entfernt", "date": "%d.%m.%Y", "suffix": "Sicherheitsupdate",
           "intro": "Gleiches Programm wie {version}, nur aktualisierte Pakete (monatliches Sicherheitsupdate am Patchday, "
                    "automatisch gebaut, getestet und mit Trivy geprüft):",
           "first": "- Erster Stand mit vollständiger Paketliste ({deb} Debian-Pakete, {pip} Python-Pakete, siehe "
                    "`/app/image-packages.txt`) – ein Vergleich mit dem vorigen Image ist nicht möglich."},
    "en": {"kinds": {"base": "Base image", "python": "Python", "deb": "Debian packages", "pip": "Python packages"},
           "new": "new", "removed": "removed", "date": "%Y-%m-%d", "suffix": "security update",
           "intro": "Same program as {version}, only updated packages (monthly security update on patch day, built, tested and "
                    "checked with Trivy automatically):",
           "first": "- First state with a complete package list ({deb} Debian packages, {pip} Python packages, see "
                    "`/app/image-packages.txt`) – a comparison with the previous image is not possible."},
}


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


Change = tuple[str, str, str, str]   # (kind, name, old version, new version)


def diff(old: str, new: str) -> list[Change]:
    """[(kind, name, old version or '', new version or '')] – sorted by kind and name."""
    a, b = parse(old), parse(new)
    changes = []
    for key in sorted(set(a) | set(b), key=lambda k: (list(KINDS).index(k[0]), k[1])):
        if a.get(key) != b.get(key):
            changes.append((key[0], key[1], a.get(key, ""), b.get(key, "")))
    return changes


def format_changes(changes: list[Change], lang: str = "de") -> list[str]:
    t = TEXTS[lang]
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
                parts.append(f"{label}{t['new']} {new}")
            else:
                parts.append(f"{label}{t['removed']} ({old})")
        lines.append(f"- {t['kinds'][kind]}: " + ", ".join(f"`{p}`" for p in parts))
    return lines


def next_tag(version: str, text: str | None = None) -> str:
    text = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    used = [int(n) for n in re.findall(rf"^## {re.escape(version)}-s(\d+) ", text, re.M)]
    return f"{version}-s{max(used, default=0) + 1}"


def changelog_entry(tag: str, version: str, changes: list[Change], today: dt.date | None = None,
                    first: bool = False, lang: str = "de") -> str:
    today = today or dt.date.today()
    t = TEXTS[lang]
    head = [f"## {tag} – {today.strftime(t['date'])} ({t['suffix']})", t["intro"].format(version=version)]
    if first:     # the previous image had no package list – nothing to compare with
        new = [c for c in changes if c[3]]
        counts = {k: sum(1 for c in new if c[0] == k) for k in ("deb", "pip")}
        body = [t["first"].format(deb=counts["deb"], pip=counts["pip"]),
                *format_changes([c for c in changes if c[0] in ("base", "python")], lang)]
    else:
        body = format_changes(changes, lang)
    return "\n".join([*head, *body, ""]) + "\n"


def insert_changelog(entry: str, text: str) -> str:
    i = text.find("\n## ")
    if i == -1:
        return text.rstrip("\n") + "\n\n" + entry
    return text[:i + 1] + entry + text[i + 1:]


def vtuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", v)
    if m is None:
        raise ValueError(f"not a version number: {v!r}")
    return tuple(int(x) for x in m.group(1).split("."))


def image_label(inspect_json: str, key: str) -> str | None:
    """Label `key` from "docker buildx imagetools inspect --format '{{json .Image}}'" – one image, or a map
    platform → image for a multi-platform index (all platforms carry the same labels). "" if the label is missing,
    None if the output is not readable."""
    try:
        data = json.loads(inspect_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    images = [data] if "config" in data else [v for v in data.values() if isinstance(v, dict) and "config" in v]
    if not images:
        return None
    labels = (images[0].get("config") or {}).get("Labels") or {}
    return str(labels.get(key, ""))


def main(argv: list[str] | None = None) -> int:
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
        old_text = Path(old).read_text()
        changes = diff(old_text, Path(new).read_text())
        if not changes:
            print("no changes – no change log entry")
            return UNCHANGED
        first = not parse(old_text)
        missing = [p.name for p in (CHANGELOG, CHANGELOG_EN) if not p.exists()]
        if missing:   # both change logs or none – never only the German one
            print(f"missing: {', '.join(missing)} – no change log entry written", file=sys.stderr)
            return 1
        for path, lang in ((CHANGELOG, "de"), (CHANGELOG_EN, "en")):
            if path.exists():
                path.write_text(insert_changelog(changelog_entry(tag, version, changes, first=first, lang=lang),
                                                 path.read_text(encoding="utf-8")), encoding="utf-8")
        return 0
    if cmd == "highest" and len(argv) == 1:
        versions = [t.strip() for t in sys.stdin.read().split() if re.fullmatch(r"v?\d+\.\d+\.\d+", t.strip())]
        if not versions:
            return 1
        print(max(versions, key=vtuple).lstrip("v"))
        return 0
    if cmd == "is-highest" and len(argv) == 3:
        return 0 if vtuple(argv[1]) >= vtuple(argv[2]) else 1
    if cmd == "label" and len(argv) == 2:
        value = image_label(sys.stdin.read(), argv[1])
        if value is None:
            return 1
        print(value)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
