"""Internes Netz oder Zugriff von außen?

Grundlage ist die echte Adresse des Besuchers. Hinter einem Reverse-Proxy übernimmt proxy.py sie aus
X-Forwarded-For – aber nur, wenn die Verbindung von einem vertrauenswürdigen Proxy kommt.

Sicherheitsregeln:
* Kommt eine Anfrage über den Proxy, ohne dass er die Besucher-Adresse mitschickt, gilt sie als
  **extern** – sonst sähe jeder Zugriff aus dem Internet wie der (interne) Proxy aus.
* Im Zweifel (Adresse nicht lesbar) gilt: extern.
"""
from __future__ import annotations

import ipaddress
import logging

from flask import has_request_context, request

from .i18n import _
from .models import get_setting

log = logging.getLogger(__name__)

# Ohne 172.16.0.0/12: Dort legt Docker seine Netze an – sonst gälte z. B. jeder Zugriff, der über das
# Docker-Gateway (172.17.0.1 …) ankommt, als intern. Wer sein LAN dort hat, trägt es ausdrücklich ein.
DEFAULT_INTERNAL = "10.0.0.0/8\n192.168.0.0/16\nfc00::/7"
LOOPBACK = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def parse(text: str) -> tuple[list, list[str]]:
    """Netze aus Text (eine pro Zeile, auch mit Komma); liefert (Netze, ungültige Einträge)."""
    nets, bad = [], []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            bad.append(part)
    return nets, bad


def internal_networks() -> list:
    text = get_setting("internal_networks")
    nets, _ = parse(text if text.strip() else DEFAULT_INTERNAL)
    return nets


def client_ip() -> str:
    return (request.remote_addr or "") if has_request_context() else ""


def via_proxy_without_client_ip() -> bool:
    """Proxy-Angaben vorhanden, aber keine Besucher-Adresse – dann ist die Herkunft unbekannt."""
    env = request.environ
    orig = env.get("werkzeug.proxy_fix.orig") or {}
    came_via_proxy = any(env.get(h) or orig.get(h) for h in
                         ("HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED"))
    return came_via_proxy and not (env.get("HTTP_X_FORWARDED_FOR") or orig.get("HTTP_X_FORWARDED_FOR"))


# ---------------------------------------------------------------- eigener Internetanschluss

HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$", 2)
RESOLVE_TTL = 300          # Sekunden: DynDNS-Namen alle 5 Minuten neu auflösen
_cache: dict[str, tuple[float, set]] = {}


def parse_own(text: str) -> tuple[list[str], list[str]]:
    """Einträge für den eigenen Anschluss: IP-Adressen oder DNS-Namen; liefert (gültige, ungültige)."""
    ok, bad = [], []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip().lower()
        if not part:
            continue
        try:
            ok.append(str(ipaddress.ip_address(part)))
        except ValueError:
            (ok if HOST_RE.match(part) else bad).append(part)
    return ok, bad


def _resolve(host: str, now: float) -> set:
    import socket
    cached = _cache.get(host)
    if cached and now - cached[0] < RESOLVE_TTL:
        return cached[1]
    try:
        ips = {info[4][0].split("%")[0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError as exc:
        log.warning("Own connection: %s cannot be resolved (%s)", host, exc)
        # letzte bekannte Adresse höchstens eine Stunde weiterverwenden
        ips = cached[1] if cached and now - cached[0] < 3600 else set()
        _cache[host] = (cached[0], ips) if cached and ips else (now, ips)
        return ips
    _cache[host] = (now, ips)
    return ips


def own_public_ips() -> set:
    """Aktuelle öffentliche Adresse(n) des eigenen Anschlusses (feste IP oder DynDNS-Name)."""
    import time
    entries, _ = parse_own(get_setting("own_public_hosts"))
    now = time.time()
    ips = set()
    for e in entries:
        try:
            ips.add(str(ipaddress.ip_address(e)))
        except ValueError:
            ips |= _resolve(e, now)
    return ips


# ---------------------------------------------------------------- Einstufung

def _client_address():
    try:
        ip = ipaddress.ip_address(client_ip().split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def classify() -> str:
    """'lan' (internes Netz), 'own' (über den eigenen Internetanschluss) oder 'extern'."""
    if not has_request_context() or via_proxy_without_client_ip():
        return "extern"
    ip = _client_address()
    if ip is None:
        return "extern"
    if any(ip in n for n in internal_networks() + LOOPBACK):
        return "lan"
    if str(ip) in own_public_ips():
        return "own"
    return "extern"


def is_internal() -> bool:
    return classify() != "extern"


def describe() -> dict:
    """Für die Einstellungsseite: wie wird die aktuelle Verbindung eingestuft?"""
    zone = classify()
    return {"ip": client_ip() or _("unbekannt"), "internal": zone != "extern", "zone": zone,
            "own_ips": sorted(own_public_ips()),
            "proxy_without_ip": has_request_context() and via_proxy_without_client_ip()}
