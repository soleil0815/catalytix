"""Internal network or external access?

This is based on the visitor's real address. Behind a reverse proxy, proxy.py takes it from
X-Forwarded-For – but only if the connection comes from a trusted proxy.

Security rules:
* If a request comes through the proxy without the proxy passing along the visitor's address, it counts as
  **external** – otherwise every access from the internet would look like the (internal) proxy.
* When in doubt (address not readable): external.
"""
from __future__ import annotations

import ipaddress
import logging

from flask import has_request_context, request

from .i18n import _
from .models import get_setting

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Without 172.16.0.0/12: Docker creates its networks there – otherwise e.g. any access arriving via the
# Docker gateway (172.17.0.1 …) would count as internal. Anyone whose LAN is there adds it explicitly.
DEFAULT_INTERNAL = "10.0.0.0/8\n192.168.0.0/16\nfc00::/7"
LOOPBACK: list[Network] = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def parse(text: str) -> tuple[list[Network], list[str]]:
    """Networks from text (one per line, commas allowed too); returns (networks, invalid entries)."""
    nets: list[Network] = []
    bad: list[str] = []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            bad.append(part)
    return nets, bad


def internal_networks() -> list[Network]:
    text = get_setting("internal_networks")
    nets, _ = parse(text if text.strip() else DEFAULT_INTERNAL)
    return nets


def client_ip() -> str:
    return (request.remote_addr or "") if has_request_context() else ""


def via_proxy_without_client_ip() -> bool:
    """Proxy headers present but no visitor address – then the origin is unknown."""
    env = request.environ
    orig = env.get("werkzeug.proxy_fix.orig") or {}
    came_via_proxy = any(env.get(h) or orig.get(h) for h in
                         ("HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED"))
    return came_via_proxy and not (env.get("HTTP_X_FORWARDED_FOR") or orig.get("HTTP_X_FORWARDED_FOR"))


# ---------------------------------------------------------------- own internet connection

HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$", 2)
RESOLVE_TTL = 300          # seconds: re-resolve DynDNS names every 5 minutes
_cache: dict[str, tuple[float, set[str]]] = {}


def parse_own(text: str) -> tuple[list[str], list[str]]:
    """Entries for the own connection: IP addresses or DNS names; returns (valid, invalid)."""
    ok: list[str] = []
    bad: list[str] = []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip().lower()
        if not part:
            continue
        try:
            ok.append(str(ipaddress.ip_address(part)))
        except ValueError:
            (ok if HOST_RE.match(part) else bad).append(part)
    return ok, bad


def _resolve(host: str, now: float) -> set[str]:
    import socket
    cached = _cache.get(host)
    if cached and now - cached[0] < RESOLVE_TTL:
        return cached[1]
    try:
        ips = {str(info[4][0]).split("%")[0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError as exc:
        log.warning("Own connection: %s cannot be resolved (%s)", host, exc)
        # keep using the last known address for at most one hour
        ips = cached[1] if cached and now - cached[0] < 3600 else set[str]()
        _cache[host] = (cached[0], ips) if cached and ips else (now, ips)
        return ips
    _cache[host] = (now, ips)
    return ips


def own_public_ips() -> set[str]:
    """Current public address(es) of the own connection (static IP or DynDNS name)."""
    import time
    entries, _ = parse_own(get_setting("own_public_hosts"))
    now = time.time()
    ips: set[str] = set()
    for e in entries:
        try:
            ips.add(str(ipaddress.ip_address(e)))
        except ValueError:
            ips |= _resolve(e, now)
    return ips


# ---------------------------------------------------------------- Classification

def _client_address() -> Address | None:
    try:
        ip: Address = ipaddress.ip_address(client_ip().split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def classify() -> str:
    """'lan' (internal network), 'own' (via the own internet connection) or 'extern'."""
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


def describe() -> dict[str, object]:
    """For the settings page: how is the current connection classified?"""
    zone = classify()
    return {"ip": client_ip() or _("unbekannt"), "internal": zone != "extern", "zone": zone,
            "own_ips": sorted(own_public_ips()),
            "proxy_without_ip": has_request_context() and via_proxy_without_client_ip()}
