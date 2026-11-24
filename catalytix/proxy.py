"""Automatic operation behind a reverse proxy – without configuration.

* X-Forwarded-For/-Proto/-Host are only accepted from trusted senders.
  Default: private networks (10/8, 172.16/12, 192.168/16, 127/8, fc00::/7, ::1) – that is
  practically always where the reverse proxy is (e.g. Nginx Proxy Manager in the same Docker network).
  Requests coming directly from the internet therefore cannot forge the headers.
* Cookies get the Secure flag automatically as soon as the request came via HTTPS
  (directly or according to the proxy). Login still works via http://IP:9300 on the LAN.
* The protocol comes from X-Forwarded-Proto; a proxy that only sends X-Forwarded-Port counts with port 80 as HTTP
  and with 443 as HTTPS.

The same rules apply in Catalytix, AssetLotse and Helfi.Space (same variables, same behaviour).

Environment variables (optional):
  CATALYTIX_TRUSTED_PROXIES  "private" (default), "none" or a list of networks/IPs,
                               e.g. "172.18.0.0/16, 10.0.10.11"
  CATALYTIX_SECURE_COOKIES   "auto" (default), "1" (always) or "0" (never)
  CATALYTIX_FORCE_HTTPS      HTTPS through the reverse proxy, for proxies that cannot redirect or send HSTS
                               themselves (e.g. the Synology reverse proxy):
                               "auto" (default) – a host is switched to HTTPS by itself as soon as it has been
                               reached once over HTTPS through the proxy (remembered in the data folder per host
                               and proxy address, so it holds after a restart and another LAN computer cannot
                               switch it on for everyone); a proxy without HTTPS is never redirected into nothing.
                               "1" – always, "0" – never.
                               Switched to HTTPS: HTTP requests through the proxy are redirected. HTTPS responses
                               carry HSTS (one year, this host only – no includeSubDomains, other programs on
                               sibling subdomains stay unaffected) once the program had to redirect itself – a
                               proxy that redirects on its own (NPM "Force SSL") keeps its own HSTS header.
                               "1" always sends HSTS. Direct requests without proxy headers (health
                               check, http://IP:9300 on the LAN) and IP addresses are never redirected.
                               Redirects: 301 for GET/HEAD, 308 for everything else (forms keep their data).
  CATALYTIX_BEHIND_PROXY     deprecated; "0" is equivalent to TRUSTED_PROXIES=none
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from urllib.parse import quote

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
    """Applies ProxyFix only if the connection comes from a trusted proxy."""

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
            if not environ.get("HTTP_X_FORWARDED_PROTO"):
                # some proxies only report the port: 80 = HTTP, 443 = HTTPS (the protocol header wins)
                port = (environ.get("HTTP_X_FORWARDED_PORT") or "").split(",")[-1].strip()
                if port in ("80", "443"):
                    environ["HTTP_X_FORWARDED_PROTO"] = "http" if port == "80" else "https"
            if environ.get("HTTP_X_FORWARDED_PROTO"):
                environ[VIA_PROXY] = True      # for ForceHttps: this request came through the reverse proxy
                environ[PROXY_ADDR] = _addr(environ.get("REMOTE_ADDR"))   # ProxyFix replaces REMOTE_ADDR
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


VIA_PROXY = "catalytix.via_proxy"
PROXY_ADDR = "catalytix.proxy_addr"
HSTS = "max-age=31536000"
# a DNS name (not an IP address, not "localhost"): only then is https://<host>/ a sensible target
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")


MAX_HOSTS = 50               # remembered HTTPS hosts per proxy address (the proxy only forwards its configured names)
MAX_ENTRIES = 500            # all entries together


def _addr(value: str | None) -> str:
    """Normalised sender address of the proxy ("" if it is not an IP address)."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address((value or "").split("%")[0])
    except ValueError:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


class HttpsHosts:
    """What the program has seen through the reverse proxy, per host AND per proxy address – file in the data
    folder: a line "<host> <proxy address>" = reached over HTTPS through this proxy, "... redirected" = in addition,
    an HTTP request arrived through it and had to be redirected (the proxy does not redirect itself, so it presumably
    sends no HSTS either).
    Bound to the proxy address (3.5.1): with the default TRUSTED_PROXIES=private every LAN client counts as a proxy;
    before, one request from any LAN computer ("X-Forwarded-Proto: https", any Host) switched a host to HTTPS for
    everyone – users coming through a proxy without HTTPS were then redirected into nothing. Now a sender only
    switches HTTPS on for itself. Lines of older versions (without address) are ignored and simply learned again."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.hosts: set[tuple[str, str]] = set()
        self.redirected: set[tuple[str, str]] = set()
        self.mtime = 0.0
        self._lock = threading.Lock()
        self._reload()

    def _reload(self) -> None:
        if self.path is None:
            return
        try:
            st = self.path.stat()
        except OSError:
            return
        if st.st_mtime != self.mtime:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return
            hosts: set[tuple[str, str]] = set()
            redirected: set[tuple[str, str]] = set()
            for line in lines:
                parts = line.split()
                if len(parts) >= 2 and _HOST.match(parts[0]) and _addr(parts[1]) == parts[1]:
                    key = (parts[0], parts[1])
                    hosts.add(key)
                    if "redirected" in parts[2:]:
                        redirected.add(key)
            self.hosts, self.redirected, self.mtime = hosts, redirected, st.st_mtime

    def known(self, host: str, addr: str) -> bool:
        if (host, addr) not in self.hosts:
            self._reload()             # another worker may have learned it meanwhile
        return (host, addr) in self.hosts

    def was_redirected(self, host: str, addr: str) -> bool:
        if (host, addr) not in self.redirected:
            self._reload()
        return (host, addr) in self.redirected

    def _save(self, message: str, host: str, addr: str) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            lines = [f"{h} {a}" + (" redirected" if (h, a) in self.redirected else "") for h, a in sorted(self.hosts)]
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            self.mtime = self.path.stat().st_mtime
            log.info(message, host, addr)
        except OSError as e:
            log.warning("Could not remember the HTTPS host: %s", e)

    def learn(self, host: str, addr: str) -> None:
        key = (host, addr)
        if key in self.hosts or not _HOST.match(host) or not addr:
            return
        with self._lock:
            self._reload()
            if key in self.hosts or sum(1 for _, a in self.hosts if a == addr) >= MAX_HOSTS:
                return
            if len(self.hosts) >= MAX_ENTRIES:
                # full: make room at the sender with the most entries – a real proxy forwards only a few names,
                # so senders inventing names can never keep it from learning its own
                counts: dict[str, int] = {}
                for _, a in self.hosts:
                    counts[a] = counts.get(a, 0) + 1
                busiest = max(counts, key=lambda a: counts[a])
                drop = sorted(k for k in self.hosts if k[1] == busiest)[0]
                self.hosts.discard(drop)
                self.redirected.discard(drop)
            self.hosts.add(key)
            self._save("HTTPS through the reverse proxy detected for %s (proxy %s) – HTTP is now redirected to HTTPS",
                       host, addr)

    def mark_redirected(self, host: str, addr: str) -> None:
        key = (host, addr)
        if key in self.redirected or key not in self.hosts:
            return
        with self._lock:
            self._reload()
            self.redirected.add(key)
            self._save("The reverse proxy does not redirect %s to HTTPS itself (proxy %s) – HSTS is now sent",
                       host, addr)


def _host(environ: WSGIEnvironment) -> str:
    """Host name of the request without port, lower case; "" for IPv6 literals."""
    host = (environ.get("HTTP_HOST") or "").strip().lower()
    if host.startswith("["):
        return ""
    return host.rsplit(":", 1)[0] if ":" in host else host


class ForceHttps:
    """Switch to HTTPS behind a reverse proxy (CATALYTIX_FORCE_HTTPS, see the module docstring): redirect HTTP
    requests that came through the proxy to HTTPS and send HSTS on HTTPS responses of hosts the program had to
    redirect itself. Runs inside TrustedProxyFix, so wsgi.url_scheme already reflects X-Forwarded-Proto."""

    def __init__(self, app: WSGIApplication, mode: str, hosts: HttpsHosts | None = None) -> None:
        self.app = app
        self.mode = mode if mode in ("auto", "1", "0") else "auto"
        self.hosts = hosts or HttpsHosts(None)

    def _active(self, host: str, addr: str) -> bool:
        return self.mode == "1" or (self.mode == "auto" and self.hosts.known(host, addr))

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if self.mode == "0" or not environ.get(VIA_PROXY):
            return self.app(environ, start_response)      # direct request: never redirected, no HSTS
        scheme = environ.get("wsgi.url_scheme")
        host = _host(environ)
        addr = environ.get(PROXY_ADDR) or ""
        if scheme == "https" and self.mode == "auto":
            self.hosts.learn(host, addr)
        if scheme == "http" and self._active(host, addr):
            if _HOST.match(host):
                if self.mode == "auto":
                    self.hosts.mark_redirected(host, addr)
                path = quote(environ.get("PATH_INFO") or "/", safe="/%:@!$&'()*+,;=-._~")
                query = quote(environ.get("QUERY_STRING") or "", safe="=&%+;/:@!$'()*,?-._~")
                target = f"https://{host}{environ.get('SCRIPT_NAME', '')}{path}" + (f"?{query}" if query else "")
                # 301 for reading requests, 308 keeps the method and body of a form submission
                status = "301 Moved Permanently" if environ.get("REQUEST_METHOD") in ("GET", "HEAD") \
                    else "308 Permanent Redirect"
                start_response(status, [("Location", target), ("Content-Type", "text/plain; charset=utf-8"),
                                        ("Content-Length", "0"), ("Cache-Control", "no-store")])
                return [b""]
        # HSTS only where the program had to redirect itself: a proxy that redirects on its own (Nginx Proxy Manager
        # with "Force SSL") usually also sends HSTS – a second header would override its setting (browsers use the
        # first one) and is reported as an error by checkers such as hstspreload.org.
        hsts = self.mode == "1" or (self.mode == "auto" and self.hosts.was_redirected(host, addr))
        if scheme != "https" or not hsts:
            return self.app(environ, start_response)

        def _start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            if not any(name.lower() == "strict-transport-security" for name, _ in headers):
                headers = [*headers, ("Strict-Transport-Security", HSTS)]
            return start_response(status, headers, exc_info)

        return self.app(environ, _start)


class SecureCookies:
    """Sets the Secure flag on all cookies for HTTPS requests."""

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
    # Default (all private networks count as proxies)? Then the 2FA setting warns if the internal network
    # gets by without a second factor – see README "Reverse-Proxy".
    app.config["TRUSTED_PROXIES_DEFAULT"] = (trusted or "private").strip().lower() == "private"
    force = (env.get("CATALYTIX_FORCE_HTTPS") or "auto").strip().lower()
    force = {"true": "1", "yes": "1", "on": "1", "false": "0", "no": "0", "off": "0"}.get(force, force)
    app.config["FORCE_HTTPS"] = force
    hosts = HttpsHosts(Path(app.instance_path) / "https-hosts.txt")
    # order: first evaluate proxy headers (sets https), then redirect / HSTS, then adjust cookies
    inner = ForceHttps(SecureCookies(app.wsgi_app, mode), force, hosts)
    app.wsgi_app = TrustedProxyFix(inner, networks)  # type: ignore[method-assign]
