"""Automatischer Betrieb hinter einem Reverse-Proxy – ohne Konfiguration.

* X-Forwarded-For/-Proto/-Host werden nur von vertrauenswürdigen Absendern übernommen.
  Standard: private Netze (10/8, 172.16/12, 192.168/16, 127/8, fc00::/7, ::1) – dort steht
  praktisch immer der Reverse-Proxy (z. B. Nginx Proxy Manager im selben Docker-Netz).
  Aus dem Internet direkt kommende Anfragen können die Header daher nicht fälschen.
* Cookies erhalten das Secure-Flag automatisch, sobald die Anfrage per HTTPS kam
  (direkt oder laut Proxy). Über http://IP:9300 im LAN funktioniert die Anmeldung weiterhin.

Umgebungsvariablen (optional):
  CATALYTIX_TRUSTED_PROXIES  "private" (Standard), "none" oder Liste von Netzen/IPs,
                               z. B. "172.18.0.0/16, 192.168.0.11"
  CATALYTIX_SECURE_COOKIES   "auto" (Standard), "1" (immer) oder "0" (nie)
  CATALYTIX_BEHIND_PROXY     veraltet; "0" entspricht TRUSTED_PROXIES=none
"""
from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from werkzeug.middleware.proxy_fix import ProxyFix

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from flask import Flask

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128"]


def parse_networks(value: str | None) -> list[Network]:
    value = (value or "private").strip().lower()
    if value in ("none", "off", "0", "no", ""):
        return []
    nets: list[Network] = []
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "private":
            nets += [ipaddress.ip_network(n) for n in PRIVATE_NETS]
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("CATALYTIX_TRUSTED_PROXIES: invalid entry %r ignored", part)
    return nets


class TrustedProxyFix:
    """Wendet ProxyFix nur an, wenn die Verbindung von einem vertrauenswürdigen Proxy kommt."""

    def __init__(self, app: WSGIApplication, networks: list[Network]) -> None:
        self.app = app
        self.networks = networks
        self.fixed = ProxyFix(app, x_for=1, x_proto=1, x_host=0, x_port=0)

    def _trusted(self, addr: str | None) -> bool:
        try:
            ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address((addr or "").split("%")[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.networks)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if self.networks and self._trusted(environ.get("REMOTE_ADDR")):
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


class SecureCookies:
    """Setzt bei HTTPS-Anfragen das Secure-Flag an allen Cookies."""

    def __init__(self, app: WSGIApplication, mode: str = "auto") -> None:
        self.app = app
        self.mode = mode

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        secure = self.mode == "1" or (self.mode == "auto" and environ.get("wsgi.url_scheme") == "https")
        if not secure:
            return self.app(environ, start_response)

        def _start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            new: list[tuple[str, str]] = []
            for name, value in headers:
                if name.lower() == "set-cookie" and "secure" not in [
                        part.strip().lower() for part in value.split(";")[1:]]:
                    value += "; Secure"
                new.append((name, value))
            return start_response(status, new, exc_info)

        return self.app(environ, _start)


def install(app: Flask, env: Mapping[str, str]) -> None:
    mode = (env.get("CATALYTIX_SECURE_COOKIES") or "auto").strip().lower()
    if mode not in ("auto", "1", "0"):
        mode = "auto"
    trusted = env.get("CATALYTIX_TRUSTED_PROXIES")
    legacy = env.get("CATALYTIX_BEHIND_PROXY")
    if trusted is None and legacy == "0":
        trusted = "none"
    elif trusted is None and legacy == "1":
        trusted = "private"
    networks = parse_networks(trusted)
    # Standard (alle privaten Netze gelten als Proxy)? Dann warnt die 2FA-Einstellung, falls das Hausnetz
    # ohne zweiten Faktor auskommt – siehe README „Reverse-Proxy“.
    app.config["TRUSTED_PROXIES_DEFAULT"] = (trusted or "private").strip().lower() == "private"
    # Reihenfolge: erst Proxy-Header auswerten (setzt https), dann Cookies anpassen
    app.wsgi_app = TrustedProxyFix(SecureCookies(app.wsgi_app, mode), networks)  # type: ignore[method-assign]
