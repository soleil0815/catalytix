#!/usr/bin/env python3
"""Nachgebautes „docker“ für die update.sh-Tests: Zustand in $FAKE_DOCKER_STATE (JSON), Aufrufe in $FAKE_DOCKER_LOG.

Zustand:
  images    {ref oder id: {"id", "version", "dist": {Datei: Inhalt}}}   lokal vorhandene Images
  registry  {ref: Image}                                                 was „pull“ holen kann
  container {"image": id, "status", "health", "restarts", "project"} oder null
  health    {Version: "healthy" | "unhealthy" | "exited"}               Startverhalten je Version
  run_rc    {Befehl: Rückgabewert}                                       z. B. restore-before
  mounts    [Pfade]                                                      Mounts aller Container
  compose_v2  false → „docker compose“ fehlt (docker-compose wird benutzt)
"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
LOG = Path(os.environ["FAKE_DOCKER_LOG"])
DEFAULT_IMAGE = "ghcr.io/soleil0815/catalytix"


def load():
    return json.loads(STATE.read_text())


def save(s):
    STATE.write_text(json.dumps(s))


def log(line):
    with LOG.open("a") as f:
        f.write(line + "\n")


def image(s, ref):
    return s["images"].get(ref) or next((i for i in s["images"].values() if i["id"] == ref), None)


def env_file_value(key):
    p = Path(".env")
    val = None
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith(key + "="):
                val = line.split("=", 1)[1].strip().strip('"\'')
    return val


def compose_ref():
    img = os.environ.get("CATALYTIX_IMAGE") or env_file_value("CATALYTIX_IMAGE") or DEFAULT_IMAGE
    tag = os.environ.get("CATALYTIX_TAG") or env_file_value("CATALYTIX_TAG") or "latest"
    return f"{img}:{tag}"


def compose(s, args, prog):
    project = None
    if args[:1] == ["-p"]:
        project, args = args[1], args[2:]
    if args[:1] == ["version"]:
        return 0
    ref = compose_ref()
    log(f"{prog} {'-p ' + project + ' ' if project else ''}{' '.join(args)}  [image={ref}]")
    cmd = args[0]
    if cmd == "up":
        img = image(s, ref)
        if img is None:
            if ref in s["registry"]:
                s["images"][ref] = img = s["registry"][ref]
            else:
                print(f"pull access denied for {ref}", file=sys.stderr)
                return 1
        health = s.get("health", {}).get(img["version"], "healthy")
        s["container"] = {"image": img["id"], "status": "exited" if health == "exited" else "running",
                          "health": "none" if health in ("exited", "none") else health, "restarts": 0,
                          "project": project or (s.get("container") or {}).get("project", "")}
        save(s)
        return 0
    if cmd == "stop":
        if s.get("container"):
            s["container"]["status"] = "exited"
            save(s)
        return 0
    if cmd == "run":
        img = image(s, ref)
        flask = args[args.index("flask"):] if "flask" in args else args
        sub = flask[3] if len(flask) > 3 else ""
        log(f"RUN version={img['version'] if img else '?'} {' '.join(flask)}")
        out = s.get("run_out", {}).get(sub)
        if out:
            print(out)
        return s.get("run_rc", {}).get(sub, 0)
    return 0


def main():
    args = sys.argv[1:]
    s = load()
    prog = os.path.basename(sys.argv[0])
    if prog == "docker-compose":
        return compose(s, args, "docker-compose")
    if args[:1] == ["compose"]:
        if s.get("compose_v2", True) is False:
            return 1
        return compose(s, args[1:], "docker compose")
    log("docker " + " ".join(args))
    cmd = args[0] if args else ""
    if cmd == "info":
        return 0
    if cmd == "inspect":          # docker inspect -f FMT <container>
        fmt, name = args[2], args[3]
        c = s.get("container")
        if name == "c1" and c:
            print("\n".join(s.get("mounts", [])))
            return 0
        if name != "catalytix" or not c:
            return 1
        if ".Mounts" in fmt:
            print("\n".join(s.get("mounts", [])))
        elif "com.docker.compose.project" in fmt:
            print(c.get("project", ""))
        elif ".State.Status" in fmt:
            print(f"{c['status']} {c['health']} {c['restarts']}")
        elif ".Image" in fmt:
            print(c["image"])
        return 0
    if cmd == "ps":
        if s.get("container"):
            print("c1")
        return 0
    if cmd == "image" and args[1] == "inspect":
        fmt, ref = args[3], args[4]
        img = image(s, ref)
        if img is None:
            return 1
        if ".RepoTags" in fmt:
            print("\n".join(r for r, i in s["images"].items() if i["id"] == img["id"] and ":" in r
                            and not r.startswith("sha256:")))
        elif ".Id" in fmt:
            print(img["id"])
        elif "image.version" in fmt:
            print(img["version"])
        return 0
    if cmd == "image" and args[1] == "ls":
        for ref in s["images"]:
            if ref.startswith("catalytix:"):
                print(ref)
        return 0
    if cmd == "image" and args[1] == "rm":
        s["images"].pop(args[2], None)
        save(s)
        return 0
    if cmd == "tag":
        img = image(s, args[1])
        if img is None:
            return 1
        s["images"][args[2]] = img
        save(s)
        return 0
    if cmd == "pull":
        ref = args[-1]
        if ref not in s["registry"]:
            print(f"manifest for {ref} not found", file=sys.stderr)
            return 1
        s["images"][ref] = s["registry"][ref]
        save(s)
        return 0
    if cmd == "load":
        data = json.loads(Path(args[args.index("-i") + 1]).read_text())
        s["images"][data["ref"]] = data["image"]
        save(s)
        print(f"Loaded image: {data['ref']}")
        return 0
    if cmd == "create":
        img = image(s, args[-1])
        if img is None:
            return 1
        print("cid-" + img["id"])
        return 0
    if cmd == "cp":
        src, dest = args[1], Path(args[2])
        img = next(i for i in s["images"].values() if "cid-" + i["id"] == src.split(":/")[0])
        for name, text in img.get("dist", {}).items():
            (dest / name).write_text(text)
        return 0
    if cmd in ("rm", "stop"):
        if cmd == "stop" and s.get("container"):
            s["container"]["status"] = "exited"
            save(s)
        return 0
    if cmd == "logs":
        print("Traceback: something went wrong")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
