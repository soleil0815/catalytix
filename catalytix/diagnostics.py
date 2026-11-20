"""Diagnostic package (Administration → Settings → "Wartung", or `flask --app wsgi diagnose`).

A ZIP file for troubleshooting – it is only downloaded (or written to the data folder by the CLI), never sent
anywhere by the program. It is ALWAYS anonymous:

* user names, display names, e-mail addresses, host and domain names, IP addresses, AD paths, group and location
  names and the library name are replaced by placeholders (BENUTZER-007, HOST-02, domaene-1.example …);
* the placeholders are permanent (table diag_aliases): BENUTZER-007 is the same person in every package. The mapping
  stays on the server – it can be looked up on the maintenance page, it is never part of a package;
* passwords, keys and tokens never appear – not even masked: only "set" / "not set";
* instead of personal data the package holds counts (accounts per role, active loans, failed sign-ins per day …).

The package keeps up with the program: every setting must be classified below (SECRET, NAMES or PLAIN) and every
table must be listed in TABLES – tests/test_diagnostics_340.py fails for anything new that is not.

Contents: LIESMICH.txt, system.json, https.json, settings.json, checks.json, counts.json, log.txt, errors.txt.
"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

DAYS_DEFAULT = 7
DAYS_MAX = 30
MAX_LOG_LINES = 50000
MAX_LINE = 4000             # longer log lines are cut (keeps the replacement patterns fast)
LOG_DIR = "logs"
LOG_KEEP_DAYS = 31
LOG_MAX_BYTES_PER_DAY = 20 * 1024 * 1024   # beyond that only warnings and errors are written for the rest of the day
CHECK_TIMEOUT = 8

# ---------------------------------------------------------------- classification of settings
# Never in a package – not even masked: only "gesetzt" / "leer"
SECRET = {"ldap_bind_password", "smtp_password", "google_api_key", "secret_key_check", "ldap_tls_certs",
          "ldap_ca_file", "brand_logo", "brand_favicon", "brand_bg"}
# Contain names, addresses or other text about the organisation: anonymized
NAMES = {"library_name", "ldap_server", "ldap_bind_dn", "ldap_upn_suffix", "ldap_base_dn", "ldap_user_filter",
         "ldap_login_rules", "ldap_librarian_rules", "ldap_admin_rules", "smtp_host", "smtp_user", "smtp_from",
         "smtp_reply_to", "app_base_url", "internal_networks", "own_public_hosts", "registration_domains",
         "registration_privacy_url", "imprint_url", "privacy_url", "mail_last_result", "backup_last_result",
         "retention_last_result", "dircache_result"}
# Free texts (sender name, footer, sign-in page): can name the organisation in any words – never their content,
# only "Vorgabe" or "eigener Text (N Zeichen)" (3.4.1: the sender name had slipped through as text)
FREE_TEXT = {"smtp_from_name", "mail_footer", "login_eyebrow", "login_text"}
# Group rules: every line is a group name or AD path – each becomes GRUPPE-nn
RULES = {"ldap_login_rules", "ldap_librarian_rules", "ldap_admin_rules"}
# Switches, numbers, modes, timestamps: shown as they are
PLAIN = {
    "loan_days", "max_extensions", "extension_days", "max_loans", "reader_can_borrow", "self_checkout",
    "self_return", "self_extend", "reservations_enabled", "reservation_hold_days", "reservation_max",
    "catalog_visibility", "isbn_sources", "reading_sample_google", "passkey_login", "passkey_default_on",
    "cache_covers", "ldap_enabled", "ldap_security", "ldap_verify_cert", "ldap_check_hostname", "ldap_user_attr",
    "ldap_nested_groups", "ldap_group_sync", "ldap_timeout", "ldap_cache_enabled", "ldap_cache_hours",
    "mail_enabled", "smtp_port", "smtp_security", "smtp_verify_cert", "mail_on_loan", "mail_on_return",
    "mail_reminder", "mail_reminder_days", "mail_overdue", "mail_overdue_repeat_days", "mail_overdue_max",
    "mail_digest", "mail_hour", "mail_last_run", "mail_retry_after", "backup_enabled", "backup_keep",
    "backup_hour", "backup_last_ok", "backup_last_error", "retention_months", "session_hours",
    "session_admin_minutes", "session_external_minutes", "session_warning", "session_browser_close",
    "mfa_internal", "mfa_policy", "registration_mode", "registration_verify_email", "registration_group_id",
    "registration_notify_admins", "brand_bg_dim", "brand_logo_text", "brand_logo_plate", "brand_accent",
    "brand_brass", "brand_side", "login_show_shelf", "default_language", "setup_completed", "dircache_ok_at",
    "dircache_retry_after", "dircache_running", "dircache_slot", "db_version", "db_upgrade_backup",
    "version_history", "backup_last_run", "retention_last_run", "mail_running",
    "update_check", "update_check_result", "update_check_last_run",
}

# Every table with what the package takes from it – only counts, never rows
TABLES = {"users": "Konten", "groups": "Gruppen", "user_groups": "Gruppenzuordnungen", "books": "Bücher",
          "loans": "Ausleihen", "reservations": "Vormerkungen", "locations": "Standorte",
          "webauthn_credentials": "Sicherheitsschlüssel und Passkeys", "mail_log": "E-Mail-Protokoll",
          "login_attempts": "Fehlanmeldungen", "directory_people": "AD-Personen (Zwischenspeicher)",
          "settings": "Einstellungen", "diag_aliases": "Platzhalter des Diagnosepakets"}

# Words never replaced, even if a name happens to be one of them
COMMON_WORDS = {"admin", "administrator", "administrators", "administratoren", "test", "user", "users", "system",
                "gast", "guest", "service", "default", "local", "domain", "server", "leser", "reader", "librarian",
                "bibliothek", "library", "catalytix", "passkey", "login", "mail", "info", "support", "none", "true",
                "false", "localhost", "example", "invalid", "office", "schule", "school"}

KINDS = {"user": "BENUTZER-{:03d}", "mail": "mail-{:03d}@example.invalid", "host": "host-{:02d}",
         "domain": "domaene-{}.example", "ip": "IP-{:03d}", "group": "GRUPPE-{:02d}", "ou": "OU-{:02d}",
         "location": "STANDORT-{:02d}", "name": "NAME-{:03d}", "library": "BIBLIOTHEK-{}", "text": "TEXT-{:02d}",
         "account": "KONTO-{:02d}"}

# all patterns bounded (no nested or adjacent unbounded repetition): log lines come partly from outside
EMAIL_RX = re.compile(r"(?<![A-Za-z0-9._%+'-])[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
                      r"\.[A-Za-z]{2,24}(?![A-Za-z])")
IPV4_RX = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
IPV6_RX = re.compile(r"(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")   # checked by ipaddress
PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                                                  "169.254.0.0/16", "fc00::/7", "fe80::/10")]
FQDN_RX = re.compile(r"(?<![\w.@-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,10}(?:[A-Za-z]{2,24}))"
                     r"(?![\w.-])")
DN_PART_RX = re.compile(r"\b(CN|OU|DC|O|L)=((?:\\.|[^,;+\"\\=\n])+)", re.I)
# a whole AD path (bounded): replaced as one, so the same domain looks the same everywhere
DN_FULL_RX = re.compile(r"\b(?:(?:CN|OU|O|L)=(?:\\.|[^,;+\"\\=\n]){1,200},[ ]?){0,15}DC=[\w-]{1,63}"
                        r"(?:,[ ]?DC=[\w-]{1,63}){0,6}", re.I)
# value after a secret-looking word (password=…, "token": "…", Authorization: Bearer …)
SECRET_RX = re.compile(r"(?i)(pass(?:word|wort)?|pwd|secret|token|api[_-]?key|key|authorization|bearer|cookie|"
                       r"session|totp|recovery)([ \t]{0,4}[=:][ \t]{0,4}|\"[ \t]{0,4}:[ \t]{0,4}\"|"
                       r"'[ \t]{0,4}:[ \t]{0,4}')(\"[^\"]{0,500}\"|'[^']{0,500}'|[^\s,;&}\]]{1,500})")
FERNET_RX = re.compile(r"\b(?:enc:)?gAAAAA[A-Za-z0-9_=-]{20,}")
GOOGLE_KEY_RX = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")
SETUP_CODE_RX = re.compile(r"(?i)((?:setup[ -]?code|einrichtungscode)[ \t]{0,4}[:=]?[ \t]{0,4})[A-Z0-9]{4}"
                           r"(?:-[A-Z0-9]{4}){1,3}")
TLDS_KEEP = {"example", "invalid", "localhost"}   # already placeholders or generic
# Host names found only in free text (logs) are replaced when they end in one of these – names from the settings
# and the directory are learned beforehand whatever their ending. (Without this list every "db.session.commit" in
# a traceback would look like a host name.)
TLDS = {"de", "com", "org", "net", "local", "lan", "intern", "internal", "intranet", "corp", "home", "localdomain",
        "at", "ch", "eu", "io", "info", "biz", "edu", "gov", "uk", "fr", "nl", "it", "es", "be", "pl", "cz", "dk",
        "se", "no", "fi", "lu", "li", "us", "ca", "au", "cloud", "app", "dev", "online", "schule", "school", "ag",
        "gmbh", "berlin", "hamburg", "bayern", "nrw", "koeln", "sachsen", "ruhr", "saarland", "tirol", "wien", "me",
        "tv", "cc", "co", "xyz", "site", "tech", "digital", "network", "email", "dyndns", "ddns", "myfritz",
        "synology", "lan", "arpa"}
FILE_SUFFIXES = {"py", "html", "js", "css", "json", "txt", "zip", "png", "jpg", "jpeg", "svg", "db", "log", "csv",
                 "pdf", "gz", "sqlite", "ini", "yml", "yaml", "sh", "md", "webp", "ico", "lock", "pem", "key"}

_lock = threading.Lock()
STARTED = time.time()          # this process (for "running since")


# ================================================================ log file
_log_dirs: set[str] = set()


class _DailyFileHandler(logging.Handler):
    """Program log in the data folder: one file per day (logs/catalytix-YYYY-MM-DD.log), kept 31 days. Several
    worker processes append to the same file (small appends are atomic). Per day at most 20 MB – after that only
    warnings and errors, so a flood of messages cannot fill the disk."""

    def __init__(self, folder: Path):
        super().__init__(logging.INFO)
        self.folder = folder
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "werkzeug" and record.levelno < logging.WARNING:
            return            # request lines of the development server (addresses, search words in URLs)
        try:
            path = self.folder / f"catalytix-{date.today():%Y-%m-%d}.log"
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > LOG_MAX_BYTES_PER_DAY and record.levelno < logging.WARNING:
                return
            line = self.format(record).replace("\r", "") + "\n"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:  # noqa: BLE001 – logging must never break the program
            pass


def install_log_file(instance: Path) -> None:
    """Write the program log additionally into the data folder (for the diagnostic package). Once per folder."""
    folder = instance / LOG_DIR
    key = str(folder)
    if key in _log_dirs:
        return
    try:
        folder.mkdir(exist_ok=True)
        cutoff = date.today() - timedelta(days=LOG_KEEP_DAYS)
        for p in folder.glob("catalytix-*.log"):
            m = re.fullmatch(r"catalytix-(\d{4}-\d{2}-\d{2})\.log", p.name)
            if m and date.fromisoformat(m.group(1)) < cutoff:
                p.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Log folder %s not usable (%s) – the diagnostic package will have no log", folder, exc)
        return
    _log_dirs.add(key)
    handler = _DailyFileHandler(folder)
    handler._catalytix_folder = key  # type: ignore[attr-defined]
    logging.getLogger().addHandler(handler)


def uninstall_log_file(instance: Path) -> None:
    """For tests: stop writing into this folder."""
    key = str(instance / LOG_DIR)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_catalytix_folder", None) == key:
            root.removeHandler(h)
    _log_dirs.discard(key)


def log_lines(instance: Path, days: int, now: datetime | None = None) -> list[str]:
    """Raw log lines of the last `days` days (oldest first), at most MAX_LOG_LINES (the newest)."""
    now = now or datetime.now()
    first = (now - timedelta(days=days)).date()
    lines: list[str] = []
    folder = instance / LOG_DIR
    for p in sorted(folder.glob("catalytix-*.log")) if folder.is_dir() else []:
        m = re.fullmatch(r"catalytix-(\d{4}-\d{2}-\d{2})\.log", p.name)
        if not m or date.fromisoformat(m.group(1)) < first:
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines.extend(line.rstrip("\n") for line in fh)
        except OSError:
            continue
        if len(lines) > 2 * MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]
    return lines[-MAX_LOG_LINES:]


# ================================================================ version history
def record_version(now: datetime | None = None) -> None:
    """At startup: since when which version runs (for the package – "the error started with 3.3.2")."""
    from .models import db, get_setting, set_setting
    try:
        hist = json.loads(get_setting("version_history") or "[]")
        if not isinstance(hist, list):
            hist = []
    except ValueError:
        hist = []
    if hist and isinstance(hist[-1], dict) and hist[-1].get("version") == __version__:
        return
    hist.append({"version": __version__, "since": (now or datetime.now()).isoformat(timespec="seconds")})
    set_setting("version_history", json.dumps(hist[-50:]))
    db.session.commit()


def version_history() -> list[dict[str, str]]:
    from .models import get_setting
    try:
        hist = json.loads(get_setting("version_history") or "[]")
    except ValueError:
        return []
    return [h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else []


# ================================================================ anonymization
def _norm(value: str) -> str:
    return value.strip().lower()


class Anonymizer:
    """Replaces names and addresses by permanent placeholders. Learns the real values from the database and the
    settings first, then also replaces addresses it only finds in the text (e-mails, IPs, host names, AD paths)."""

    def __init__(self) -> None:
        from .models import DiagAlias
        self._map: dict[tuple[str, str], str] = {(a.kind, a.real): a.alias for a in DiagAlias.query.all()}
        self._used: dict[str, set[str]] = {}
        for (kind, _real), alias in self._map.items():
            self._used.setdefault(kind, set()).add(alias)
        self._new: list[tuple[str, str, str]] = []
        self._words: list[tuple[str, str]] = []      # (real lower, alias), longest first
        self._domains: dict[str, str] = {}             # known domains: unknown hosts below them are replaced too
        self._regex: re.Pattern[str] | None = None

    # -- placeholders
    def alias(self, kind: str, real: str) -> str:
        key = _norm(real)
        if not key:
            return ""
        hit = self._map.get((kind, key))
        if hit:
            return hit
        used = self._used.setdefault(kind, set())
        n = len(used) + 1
        while KINDS[kind].format(n) in used:
            n += 1
        alias = KINDS[kind].format(n)
        used.add(alias)
        self._map[(kind, key)] = alias
        self._new.append((kind, key[:500], alias))
        self._regex = None
        return alias

    def save(self) -> None:
        """Remember new placeholders (so they stay the same in the next package)."""
        if not self._new:
            return
        from .models import DiagAlias, db
        with _lock:
            for kind, real, alias in self._new:
                if db.session.get(DiagAlias, (kind, real)) is None:
                    db.session.add(DiagAlias(kind=kind, real=real, alias=alias))
            db.session.commit()
        self._new = []

    # -- learning real values
    def learn(self) -> None:
        from .models import DirectoryPerson, Group, Location, User, get_setting
        for u in User.query.all():
            a = self.alias("user", u.username)
            self._add(u.username, a)
            self._add_name(u.display_name, a)
            if u.email:
                self._add(u.email, self.alias("mail", u.email))
            if u.ldap_dn:
                self.dn(u.ldap_dn)
        for p in DirectoryPerson.query.all():
            a = self.alias("user", p.username)
            self._add(p.username, a)
            self._add_name(p.display_name, a)
            if p.email:
                self._add(p.email, self.alias("mail", p.email))
        for g in Group.query.all():
            self._add(g.name, self.alias("group", g.name))
            if g.ldap_dn:
                self.dn(g.ldap_dn)
        for loc in Location.query.all():
            self._add(loc.name, self.alias("location", loc.name))
        name = get_setting("library_name")
        if name and _norm(name) not in COMMON_WORDS:
            self._add_name(name, self.alias("library", name))
        for key in ("ldap_base_dn", "ldap_bind_dn"):
            if get_setting(key):
                self.dn(get_setting(key))
        upn = (get_setting("ldap_upn_suffix") or "").lstrip("@")
        if upn:
            self._domain(upn)
        # host names in the settings – also short ones ("dc01") and any ending ("firma.intra")
        for key in ("ldap_server", "smtp_host", "own_public_hosts", "app_base_url", "imprint_url", "privacy_url",
                    "registration_domains", "registration_privacy_url"):
            for token in re.split(r"[,;\s]+", get_setting(key) or ""):
                host = re.sub(r"^[a-z]+://", "", token.strip(), flags=re.I).split("/")[0].split("@")[-1]
                host = re.sub(r":\d+$", "", host).strip("[]").strip(".")
                if not host or _is_ip(host):
                    continue
                if "." in host:
                    self._domain(host)
                elif _norm(host) not in COMMON_WORDS:
                    self._add(host, self.alias("host", host))
        smtp_user = (get_setting("smtp_user") or "").strip()
        if smtp_user and "@" not in smtp_user:
            self._add(smtp_user, self.alias("account", smtp_user))
        for key in RULES:
            for rule in re.split(r"[\n;]+", get_setting(key) or ""):
                rule = rule.strip()
                if not rule or rule.startswith("#"):
                    continue
                if "=" in rule:
                    self.dn(rule)
                else:
                    self._add(rule, self.alias("group", rule))
        # names of the organisation in free texts (sender name, footer): replaced wherever they appear
        for key in ("smtp_from_name", "mail_footer"):
            value = (get_setting(key) or "").strip()
            if value:
                self._add_name(value, self.alias("text", value))
        for key in NAMES:
            self.text(get_setting(key) or "")     # finds mails and IPs in the settings themselves

    def _add(self, real: str | None, alias: str) -> None:
        r = _norm(real or "")
        if len(r) >= 2 and r not in COMMON_WORDS and (r, alias) not in self._words:
            self._words.append((r, alias))
            self._regex = None

    def _add_name(self, display: str | None, alias: str) -> None:
        if not display:
            return
        self._add(display, alias)
        for part in re.split(r"[\s,;:|/·–—()\"„“'-]+", display):
            p = part.strip(".")
            if _norm(p) in COMMON_WORDS:
                continue
            if len(p) >= 4 or (2 <= len(p) <= 3 and p.isupper() and p.isalpha()):   # also acronyms (ABC)
                self._add(p, alias)

    def _domain(self, name: str) -> str:
        """Domain-like names keep their shape: dc01.firma.local → host-01.domaene-1.example"""
        name = name.strip(".").lower()
        labels = name.split(".")
        if len(labels) < 2 or labels[-1] in TLDS_KEEP:
            return name
        if labels[-1] in FILE_SUFFIXES:
            return name
        dom = ".".join(labels[-2:])
        dalias = self.alias("domain", dom)
        self._add(dom, dalias)
        if dom not in self._domains:
            self._domains[dom] = dalias
            self._regex = None
        if len(labels) == 2:
            return dalias
        host = ".".join(labels[:-2])
        halias = self.alias("host", host + "." + dom)
        out = f"{halias}.{dalias}"
        self._add(name, out)
        return out

    def dn(self, dn: str) -> str:
        """CN=Max Muster,OU=Lehrer,DC=firma,DC=local → CN=NAME-003,OU=OU-01,DC=domaene-1,DC=example"""
        dcs = [v for k, v in DN_PART_RX.findall(dn) if k.upper() == "DC"]
        dom_alias = self.alias("domain", ".".join(dcs[-2:])) if len(dcs) >= 2 else ""
        dom_parts = dom_alias.split(".") if dom_alias else []

        def part(m: re.Match[str]) -> str:
            k, v = m.group(1), m.group(2).strip()
            ku = k.upper()
            if ku == "DC":
                idx = dcs.index(v) if v in dcs else 0
                tail = len(dcs) - idx               # DC position from the right
                if tail <= 2 and dom_parts:
                    return f"{k}={dom_parts[len(dom_parts) - tail] if tail <= len(dom_parts) else dom_parts[0]}"
                return f"{k}={self.alias('host', v)}"
            if ku == "OU":
                return f"{k}={self.alias('ou', v)}"
            hit = self._word_alias(v)
            return f"{k}={hit or self.alias('name', v)}"
        out = DN_PART_RX.sub(part, dn)
        self._add(dn, out)
        return out

    def _word_alias(self, value: str) -> str | None:
        v = _norm(value)
        for real, alias in self._words:
            if real == v:
                return alias
        return None

    # -- free text
    def text(self, value: Any) -> str:
        t = str(value if value is not None else "")
        if len(t) > MAX_LINE:
            t = t[:MAX_LINE] + " …(gekürzt)"
        t = mask_secrets(t)
        if not t:
            return t
        if "=" in t:
            t = DN_FULL_RX.sub(lambda m: self.dn(m.group(0)), t)
            t = DN_PART_RX.sub(lambda m: self._dn_in_text(m), t)
        t = EMAIL_RX.sub(lambda m: self.alias("mail", m.group(0)), t)
        t = self._replace_hosts_below_known_domains(t)
        t = self._replace_words(t)
        t = IPV4_RX.sub(lambda m: self._ip(m.group(1)), t)
        t = IPV6_RX.sub(lambda m: self._ip(m.group(1)), t)
        t = FQDN_RX.sub(lambda m: self._fqdn(m.group(1)), t)
        return t

    def _dn_in_text(self, m: re.Match[str]) -> str:
        k, v = m.group(1), m.group(2)
        ku = k.upper()
        if ku == "DC":
            return f"{k}={self._domain_label(v)}"
        if ku == "OU":
            return f"{k}={self.alias('ou', v)}"
        return f"{k}={self._word_alias(v) or self.alias('name', v)}"

    def _domain_label(self, v: str) -> str:
        for (kind, real), alias in self._map.items():
            if kind == "domain" and v.lower() in real.split("."):
                labels = real.split(".")
                return alias.split(".")[labels.index(v.lower())] if len(labels) == len(alias.split(".")) else alias
        return self.alias("host", v)

    def _ip(self, raw: str) -> str:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return raw
        if ip.is_loopback or ip.is_unspecified:
            return raw
        if isinstance(ip, ipaddress.IPv4Address) and (raw.startswith("255.") or raw.endswith(".0")
                                                      or raw.endswith(".255")):
            return raw                                   # masks and network/broadcast addresses
        zone = "privat" if any(ip in n for n in PRIVATE_NETS if n.version == ip.version) else "öffentlich"
        return f"{self.alias('ip', raw)}({zone})"

    def _fqdn(self, name: str) -> str:
        labels = name.lower().split(".")
        if labels[-1] in FILE_SUFFIXES or labels[-1] in TLDS_KEEP or labels[-1] not in TLDS:
            return name
        if re.fullmatch(r"(host|domaene)-\d+.*", labels[0]) or name.lower().endswith(".example"):
            return name
        if labels[0] in ("catalytix", "werkzeug", "gunicorn", "sqlalchemy", "flask", "urllib3", "requests",
                         "ldap3", "fido2", "smtplib", "ssl", "jinja2", "datetime", "os", "sys", "self", "app"):
            return name                                  # module names in log lines (catalytix.mailer …)
        if name.lower() in ("books.google.com", "www.googleapis.com", "openlibrary.org", "covers.openlibrary.org",
                            "services.dnb.de", "portal.dnb.de", "github.com", "ghcr.io"):
            return name                                  # public services the program uses
        return self._domain(name)

    def _replace_hosts_below_known_domains(self, t: str) -> str:
        """pc-buchhaltung.firma.intra → host-07.domaene-1.example, even with an ending not in TLDS"""
        if not self._domains:
            return t
        rx = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,10})(" +
                        "|".join(re.escape(d) for d in sorted(self._domains, key=len, reverse=True)) + r")(?![\w-])",
                        re.I)
        return rx.sub(lambda m: self._domain(m.group(1) + m.group(2)), t)

    def _replace_words(self, t: str) -> str:
        if not self._words:
            return t
        if self._regex is None:
            words = sorted(self._words, key=lambda w: len(w[0]), reverse=True)
            self._lookup = {}
            for real, alias in words:
                self._lookup.setdefault(real, alias)
            self._regex = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w, _a in words) + r")(?![\w@-])",
                                     re.I)
        return self._regex.sub(lambda m: self._lookup.get(m.group(1).lower(), m.group(1)), t)

    def mapping(self) -> list[tuple[str, str, str]]:
        return sorted(((k, r, a) for (k, r), a in self._map.items()), key=lambda x: (x[0], x[2]))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def mask_secrets(t: str) -> str:
    """Passwords, keys and tokens in free text → ***. Always, independent of anonymization."""
    t = FERNET_RX.sub("***", t)
    t = SETUP_CODE_RX.sub(r"\1***", t)
    t = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 ***", t)
    t = GOOGLE_KEY_RX.sub("***", t)
    return SECRET_RX.sub(lambda m: f"{m.group(1)}{m.group(2)}***", t)


def lookup(query: str) -> list[tuple[str, str, str]]:
    """Maintenance page: placeholder → real value (or the other way round). (kind, alias, real)"""
    from .models import DiagAlias
    q = (query or "").strip().lower()
    if not q:
        return []
    rows = DiagAlias.query.all()
    hits = [(a.kind, a.alias, a.real) for a in rows if q in a.alias.lower() or q in a.real]
    return sorted(hits, key=lambda x: (x[0], x[1]))[:200]


def mapping_csv() -> str:
    from .models import DiagAlias
    out = io.StringIO()
    out.write("Art;Platzhalter;Echter Wert;angelegt\n")
    for a in DiagAlias.query.order_by(DiagAlias.kind, DiagAlias.alias).all():
        cells = [a.kind, a.alias, a.real, f"{a.created_at:%Y-%m-%d %H:%M}"]
        out.write(";".join('"' + c.replace('"', '""') + '"' if re.search(r'[;"\n]', c) else c for c in cells) + "\n")
    return out.getvalue()


# ================================================================ contents
def _settings(an: Anonymizer) -> dict[str, Any]:
    """Effective values of all settings (defaults included); "*" marks values changed from the default."""
    from .models import DEFAULT_SETTINGS, Setting
    stored = {row.key: row.value or "" for row in Setting.query.all()}
    out: dict[str, Any] = {}
    for key in sorted(set(DEFAULT_SETTINGS) | set(stored)):
        value = stored.get(key, DEFAULT_SETTINGS.get(key, ""))
        if key in stored and key in DEFAULT_SETTINGS and stored[key] != DEFAULT_SETTINGS[key]:
            key_out = key + " *"
        else:
            key_out = key
        out[key_out] = _setting_value(key, value, an)
    return out


def _setting_value(key: str, value: str, an: Anonymizer) -> Any:
    from .models import DEFAULT_SETTINGS
    if key in SECRET:
        return "(gesetzt)" if value else "(leer)"
    if key in FREE_TEXT:
        if not value:
            return "(leer)"
        return "(Vorgabe)" if value == DEFAULT_SETTINGS.get(key) else f"(eigener Text, {len(value)} Zeichen)"
    if key in NAMES:
        return an.text(value)
    if key in PLAIN:
        return value if key != "version_history" else "(siehe system.json)"
    return "(nicht eingeordnet – Wert weggelassen)"


def _env(an: Anonymizer) -> dict[str, str]:
    out = {}
    for k in sorted(os.environ):
        if not (k.startswith("CATALYTIX_") or k in ("DATABASE_URL", "TZ", "PUID", "PGID", "SERVER_SOFTWARE",
                                                     "GUNICORN_CMD_ARGS", "WEB_CONCURRENCY")):
            continue
        v = os.environ[k]
        if re.search(r"SECRET|PASSWORD|KEY|TOKEN", k):
            out[k] = "(gesetzt)" if v else "(leer)"
        elif k == "DATABASE_URL":
            out[k] = re.sub(r"//[^@/]*@", "//***@", an.text(v))
        else:
            out[k] = an.text(v)
    return out


def _disk(path: Path) -> dict[str, Any]:
    try:
        u = shutil.disk_usage(path)
        return {"frei_mb": u.free // 2**20, "gesamt_mb": u.total // 2**20,
                "frei_prozent": round(100 * u.free / u.total, 1) if u.total else None}
    except OSError as exc:
        return {"fehler": type(exc).__name__}


def _database(instance: Path) -> dict[str, Any]:
    from sqlalchemy import text as sql

    from .models import db
    info: dict[str, Any] = {"art": db.engine.dialect.name}
    try:
        from . import backup
        f = backup.database_file()
        if f and f.exists():
            info["groesse_mb"] = round(f.stat().st_size / 2**20, 2)
            wal = f.with_name(f.name + "-wal")
            if wal.exists():
                info["wal_mb"] = round(wal.stat().st_size / 2**20, 2)
    except Exception as exc:  # noqa: BLE001
        info["datei_fehler"] = type(exc).__name__
    if db.engine.dialect.name == "sqlite":
        try:
            info["quick_check"] = db.session.execute(sql("PRAGMA quick_check")).scalar()
            info["journal_mode"] = db.session.execute(sql("PRAGMA journal_mode")).scalar()
        except Exception as exc:  # noqa: BLE001
            info["quick_check"] = f"Fehler: {type(exc).__name__}"
    from . import dbversion
    info["schema_stempel"] = dbversion.read_stamp(db)
    return info


def _update_status() -> dict[str, Any]:
    from . import updatecheck
    st = updatecheck.status()
    return {k: st[k] for k in ("on", "checked", "ok", "error", "detail", "latest", "newer")}


def _system(instance: Path, an: Anonymizer, started: float | None) -> dict[str, Any]:
    from . import composefile
    tz = time.strftime("%Z %z")
    data: dict[str, Any] = {
        "programm": "Catalytix", "version": __version__,
        "versionsverlauf": version_history(),
        "erstellt": datetime.now().isoformat(timespec="seconds"), "zeitzone": tz,
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0], "plattform": platform.platform(terse=True),
        "architektur": platform.machine(), "prozess_laeuft_seit_min": round((time.time() - started) / 60)
        if started else None,
        "container": composefile.in_container(), "compose_version": composefile.current(),
        "compose_veraltet": composefile.outdated() is not None,
        "versionspruefung": _update_status(),
        "datenbank": _database(instance), "datenordner": _disk(instance),
        "umgebung": _env(an),
        "pakete": _packages(),
    }
    return data


def _packages() -> dict[str, str]:
    from importlib import metadata
    out = {}
    for name in ("flask", "werkzeug", "sqlalchemy", "flask-sqlalchemy", "flask-login", "flask-wtf", "ldap3", "fido2",
                 "cryptography", "requests", "gunicorn", "pyotp", "jinja2"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return out


def _https(an: Anonymizer) -> dict[str, Any]:
    """How the program sees the connection: the cause of most passkey and cookie problems."""
    from flask import current_app, has_request_context, request

    from . import twofa
    cfg = current_app.config
    data: dict[str, Any] = {
        "vertrauenswuerdige_proxys_standard": cfg.get("TRUSTED_PROXIES_DEFAULT"),
        "https_erzwingen": cfg.get("FORCE_HTTPS"),
        "sichere_cookies": os.environ.get("CATALYTIX_SECURE_COOKIES", "auto"),
    }
    hosts_file = Path(current_app.instance_path) / "https-hosts.txt"
    try:
        hosts = [h.strip() for h in hosts_file.read_text().splitlines() if h.strip()] if hosts_file.exists() else []
    except OSError:
        hosts = []
    data["auf_https_umgestellte_adressen"] = [an.text(h) for h in hosts]
    if not has_request_context():
        data["hinweis"] = ("Über die Konsole erstellt – ohne Browser-Anfrage. Für die Angaben zur Verbindung das "
                           "Paket über Einstellungen → Wartung herunterladen.")
        return data
    from . import netzone
    hdr = request.headers
    host = request.host
    rp = twofa.rp_id_for(host)
    is_ip = True
    try:
        ipaddress.ip_address(rp)
    except ValueError:
        is_ip = False
    zone = netzone.describe()
    data["anfrage"] = {
        "schema": request.scheme, "sicher": request.is_secure, "adresse": an.text(host),
        "passkey_adresse_rp_id": an.text(rp),
        "passkeys_moeglich": request.is_secure and not is_ip or rp == "localhost",
        "ueber_proxy": any(h in hdr for h in ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host",
                                              "X-Real-Ip", "Forwarded")),
        "x_forwarded_proto": hdr.get("X-Forwarded-Proto", ""),
        "x_forwarded_host_gesetzt": "X-Forwarded-Host" in hdr,
        "x_forwarded_port": hdr.get("X-Forwarded-Port", ""),
        "zone": zone.get("zone"), "intern": zone.get("internal"),
        "proxy_ohne_client_ip": zone.get("proxy_without_ip"),
        "client_ip": an.text(str(zone.get("ip") or "")),
        "browser": _browser(hdr.get("User-Agent", "")),
    }
    return data


def _browser(ua: str) -> str:
    """Kind of browser and system only (no full user agent)."""
    system = next((n for p, n in (("iPhone", "iOS"), ("iPad", "iPadOS"), ("Android", "Android"),
                                  ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux")) if p in ua), "?")
    browser = next((n for p, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                   ("Safari/", "Safari")) if p in ua), "?")
    return f"{browser} / {system}"


def _timed(fn: Callable[[], str], an: Anonymizer) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        msg = fn()
        ok = True
    except Exception as exc:  # noqa: BLE001 – every failure is a result here
        ok, msg = False, f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "meldung": an.text(msg)[:600], "dauer_ms": round((time.monotonic() - t0) * 1000)}


def _checks(an: Anonymizer, live: bool) -> dict[str, Any]:
    from .models import get_setting
    out: dict[str, Any] = {}
    on = lambda key: (get_setting(key) or "") in ("1", "true", "on", "yes")  # noqa: E731
    # Active Directory
    if not on("ldap_enabled"):
        out["active_directory"] = "aus"
    elif not get_setting("ldap_bind_dn"):
        out["active_directory"] = "an, ohne Dienstkonto"
    elif live:
        def ldap_test() -> str:
            from .ldap_auth import Directory
            return Directory().test_connection()
        out["active_directory"] = _timed(ldap_test, an)
    out["ad_zwischenspeicher"] = {"letzter_erfolg": get_setting("dircache_ok_at"),
                                  "ergebnis": an.text(get_setting("dircache_result") or "")}
    # E-mail
    if not on("mail_enabled"):
        out["email"] = "aus"
    elif live:
        def smtp_test() -> str:
            from .mailer import SmtpConfig, smtp_connect
            cfg = SmtpConfig.from_settings()
            cfg.timeout = CHECK_TIMEOUT
            s = smtp_connect(cfg)
            try:
                s.noop()
            finally:
                try:
                    s.quit()
                except Exception:  # noqa: BLE001
                    pass
            return "Verbindung und Anmeldung erfolgreich (keine E-Mail gesendet)."
        out["email"] = _timed(smtp_test, an)
    out["email_letzter_lauf"] = {"zeit": get_setting("mail_last_run"),
                                 "ergebnis": an.text(get_setting("mail_last_result") or "")}
    # Google Books
    from .isbn import google_api_key
    from .reading_sample import quota_problem
    out["google_books"] = {"leseproben": on("reading_sample_google"), "eigener_schluessel": bool(google_api_key()),
                           "tageslimit_zuletzt_erreicht": quota_problem()}
    if google_api_key() and live:
        def google_test() -> str:
            from .isbn import google_get
            data = google_get({"q": "isbn:9783161484100", "maxResults": 1, "fields": "totalItems"})
            return f"Schlüssel gültig, Antwort erhalten (totalItems={data.get('totalItems', 0)})."
        out["google_books"]["test"] = _timed(google_test, an)
    # Backup
    from . import backup
    try:
        items = backup.list_backups() if backup.supported() else []
        newest = items[0]["created"] if items else None
        out["datensicherung"] = {
            "an": on("backup_enabled"), "unterstuetzt": backup.supported(), "anzahl": len(items),
            "neueste_alter_std": round((datetime.now() - newest).total_seconds() / 3600, 1) if newest else None,
            "letzter_erfolg": get_setting("backup_last_ok"), "letzter_fehler": get_setting("backup_last_error"),
            "letztes_ergebnis": an.text(get_setting("backup_last_result") or ""),
            "warnung": an.text(backup.warning() or ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["datensicherung"] = {"fehler": type(exc).__name__}
    out["aufbewahrung_letzter_lauf"] = an.text(get_setting("retention_last_result") or "")
    out["hintergrundaufgaben"] = os.environ.get("CATALYTIX_SCHEDULER", "an (Standard)")
    if not live:
        out["hinweis"] = "Verbindungstests übersprungen."
    return out


def _counts(days: int) -> dict[str, Any]:
    from sqlalchemy import func, inspect

    from . import lockout
    from .models import Book, Loan, LoginAttempt, MailLog, Reservation, User, WebAuthnCredential, db
    now = datetime.now()
    since = now - timedelta(days=days)
    out: dict[str, Any] = {}
    tables = inspect(db.engine).get_table_names()
    out["tabellen"] = {}
    from sqlalchemy import text as sql
    for t in sorted(tables):
        try:
            out["tabellen"][t] = db.session.execute(sql(f'SELECT COUNT(*) FROM "{t}"')).scalar()
        except Exception:  # noqa: BLE001
            out["tabellen"][t] = None
    users = User.query.all()
    cred_users = {c.user_id for c in WebAuthnCredential.query.all()}
    pk_users = {c.user_id for c in WebAuthnCredential.query.filter_by(passkey=True).all()}
    out["konten"] = {
        "gesamt": len(users),
        "nach_rolle": _tally(u.role for u in users),
        "nach_herkunft": _tally(u.auth_source or "local" for u in users),
        "aktiv": sum(1 for u in users if u.active),
        "warten_auf_freigabe": sum(1 for u in users if u.pending_approval),
        "email_unbestaetigt": sum(1 for u in users if u.self_registered and not u.email_verified),
        "mit_authenticator_app": sum(1 for u in users if u.totp_enabled),
        "mit_sicherheitsschluessel_oder_passkey": len(cred_users),
        "mit_passkey": len(pk_users),
        "passkey_hinweis_ausgeblendet": sum(1 for u in users if u.passkey_hint_off),
        "ad_konto_verschwunden": sum(1 for u in users if u.ldap_gone_at),
        "angemeldet_im_zeitraum": sum(1 for u in users if u.last_login and u.last_login >= since),
        "nach_sprache": _tally(u.language or "-" for u in users),
        "derzeit_gesperrt": len(lockout.locked_usernames()),
    }
    creds = WebAuthnCredential.query.all()
    out["schluessel"] = {"passkeys": sum(1 for c in creds if c.passkey),
                         "sicherheitsschluessel": sum(1 for c in creds if not c.passkey),
                         "nach_adresse": len({c.rp_id for c in creds})}
    active = Loan.query.filter(Loan.returned_at.is_(None))
    out["ausleihen"] = {
        "laufend": active.count(),
        "ueberfaellig": active.filter(Loan.due_at < now.date()).count(),
        "im_zeitraum_ausgeliehen": Loan.query.filter(Loan.loaned_at >= since).count(),
        "im_zeitraum_zurueck": Loan.query.filter(Loan.returned_at >= since).count(),
    }
    out["buecher"] = {"titel": Book.query.count(),
                      "exemplare": int(db.session.query(func.coalesce(func.sum(Book.copies), 0)).scalar() or 0),
                      "ohne_isbn": Book.query.filter((Book.isbn.is_(None)) | (Book.isbn == "")).count()}
    out["vormerkungen"] = _tally(r.status for r in Reservation.query.all())
    out["email_im_zeitraum"] = _tally(f"{m.kind}/{m.status}" for m in
                                      MailLog.query.filter(MailLog.created_at >= since).all())
    per_day: dict[str, dict[str, int]] = {}
    for a in LoginAttempt.query.filter(LoginAttempt.created_at >= since).all():
        day = per_day.setdefault(f"{a.created_at:%Y-%m-%d}", {"passwort": 0, "zweiter_faktor": 0, "passkey": 0,
                                                              "intern": 0, "extern": 0})
        kind = "zweiter_faktor" if a.key.startswith("2fa|") else "passkey" if a.key.startswith("passkey|") \
            else "passwort"
        day[kind] += 1
        day["intern" if a.internal else "extern"] += 1
    out["fehlanmeldungen_pro_tag"] = dict(sorted(per_day.items()))
    return out


def _tally(values: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        k = str(v)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


LEVEL_RX = re.compile(r"^\d{4}-\d{2}-\d{2} [\d:,]+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")


def _errors(lines: list[str]) -> list[str]:
    """Error and warning entries with their traceback lines (already anonymized lines)."""
    out: list[str] = []
    take = False
    for line in lines:
        m = LEVEL_RX.match(line)
        if m:
            take = m.group(1) in ("WARNING", "ERROR", "CRITICAL")
            if take and out:
                out.append("")
        if take:
            out.append(line)
    return out


def _readme(days: int, live: bool, n_lines: int) -> str:
    return f"""Diagnosepaket Catalytix {__version__}
Erstellt: {datetime.now():%d.%m.%Y %H:%M} · Zeitraum: letzte {days} Tage

Dieses Paket ist anonym. Benutzer- und Anzeigenamen, E-Mail-Adressen, Rechner- und Domänennamen, IP-Adressen,
AD-Pfade, Gruppen-, Standort- und Bibliotheksnamen sind durch Platzhalter ersetzt (BENUTZER-007, host-02,
domaene-1.example, IP-004(privat) …). Die Platzhalter bleiben dauerhaft gleich; die Zuordnung liegt nur auf dem
Server (Einstellungen → Wartung → Platzhalter nachschlagen) und ist nicht Teil dieses Pakets.
Kennwörter, Schlüssel und Tokens sind nie enthalten – nur, ob sie gesetzt sind.
Statt persönlicher Daten enthält das Paket nur Zahlen. Buchtitel und Ausleihen einzelner Personen fehlen ganz.

Inhalt
  system.json    Version, Versionsverlauf, Python, Plattform, Container, Datenbank, freier Speicher, Umgebung
  https.json     wie das Programm die Verbindung sieht (HTTPS, Proxy, Passkey-Adresse, intern/extern)
  settings.json  alle Einstellungen (Geheimes nur als „gesetzt“/„leer“, Namen anonymisiert;
                 * = gegenüber der Vorgabe geändert)
  checks.json    Prüfungen: Active Directory, E-Mail-Server, Google Books, Datensicherung{"" if live else " (Verbindungstests übersprungen)"}
  counts.json    Zahlen: Konten nach Rolle/Herkunft/2FA/Passkey, Ausleihen, Bücher, Fehlanmeldungen pro Tag
  log.txt        Programmprotokoll des Zeitraums ({n_lines} Zeilen, anonymisiert)
  errors.txt     nur Warnungen und Fehler daraus, mit technischen Details
"""


def build(days: int = DAYS_DEFAULT, *, live: bool = True, started: float | None = STARTED) -> tuple[str, bytes]:
    """The ZIP file: (file name, content). Saves new placeholders."""
    from flask import current_app
    days = max(1, min(int(days or DAYS_DEFAULT), DAYS_MAX))
    instance = Path(current_app.instance_path)
    an = Anonymizer()
    an.learn()
    raw = log_lines(instance, days)
    lines = [an.text(line) for line in raw]
    files: dict[str, str] = {
        "system.json": _json(_system(instance, an, started)),
        "https.json": _json(_https(an)),
        "settings.json": _json(_settings(an)),
        "checks.json": _json(_checks(an, live)),
        "counts.json": _json(_counts(days)),
        "log.txt": "\n".join(lines) + ("\n" if lines else "(kein Protokoll im Zeitraum)\n"),
        "errors.txt": "\n".join(_errors(lines)) + "\n",
    }
    files = {"LIESMICH.txt": _readme(days, live, len(lines)), **files}
    an.save()
    name = f"catalytix-diagnose-{__version__}-{datetime.now():%Y%m%d-%H%M}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, content in files.items():
            z.writestr(f"{name[:-4]}/{fname}", content)
    log.info("Diagnostic package created (%s days, %s log lines)", days, len(lines))
    return name, buf.getvalue()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"
