"""Authentication against Active Directory (Windows Server or Samba AD) or LDAP.

Flow
  1. Connect with the service account (bind DN) – or, if none is configured,
     directly as the user via UPN (user@firma.local).
  2. Search for the user below the base DN via the login attribute
     (AD/Samba AD: sAMAccountName, OpenLDAP/classic Samba: uid).
  3. Bind with the user's DN and password (the actual password check).
  4. Determine the groups (memberOf, optionally nested via
     LDAP_MATCHING_RULE_IN_CHAIN, fallback member/uniqueMember/memberUid).
  5. Apply the rules. Each rule is either an OU/container
     (the user is located below it) or the DN of a group (the user is a member):
       - admin rules        -> role administrator
       - librarian rules    -> role librarian
       - login rules        -> role reader
     If no rule matches, the login is refused.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .i18n import _
from .models import ROLE_ADMIN, ROLE_LIBRARIAN, ROLE_READER, get_bool, get_int, get_setting

# Briefly cache the groups of people from the suggestion search: when typing on ("ann" → "anna") the same
# people come up again, but nested resolution costs several queries per person. Only for the search –
# login and re-checks always query fresh.
SEARCH_GROUP_TTL = 300
_search_groups: dict[str, tuple[float, dict]] = {}

log = logging.getLogger(__name__)
IN_CHAIN = "1.2.840.113556.1.4.1941"


class LDAPError(Exception):
    """Error with a message that administrators can understand."""


class LDAPUserNotFound(LDAPError):
    """The account does not exist (any more) in the directory."""


class LDAPAmbiguous(LDAPError):
    """The username matches several entries in the directory."""


@dataclass
class LDAPUser:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    role: str | None = None
    matched_rule: str = ""
    group_info: dict = field(default_factory=dict)  # DN -> 'direkt' | 'primär' | 'verschachtelt' (origin)
    guid: str = ""      # objectGUID (AD) or entryUUID – stays the same when renamed or moved


class ScanResult(list):
    """Result of scan_people: (LDAPUser, group_key, newly_resolved) per person, plus .uncertain:
    (username, guid) of the people whose groups could not be determined reliably."""

    def __init__(self, *a):
        super().__init__(*a)
        self.uncertain: list[tuple[str, str]] = []


def entry_guid(entry: dict) -> str:
    """Stable identifier of an entry: objectGUID (AD) or entryUUID (OpenLDAP), otherwise empty."""
    raw = (entry.get("raw_attributes") or {})
    g = raw.get("objectGUID") or []
    if g and isinstance(g[0], (bytes, bytearray)) and len(g[0]) == 16:
        return g[0].hex()
    attrs = entry.get("attributes") or {}
    u = _first(attrs.get("entryUUID"))
    return u.replace("-", "").lower() if u else ""


def normalize_dn(dn: str) -> str:
    return re.sub(r"\s*([,=])\s*", r"\1", (dn or "").strip()).lower()


def split_dn(dn: str) -> list[str]:
    r"""Split a DN into its components; "\," (and any other character escaped with \) does not split."""
    parts, cur, i = [], [], 0
    while i < len(dn):
        ch = dn[i]
        if ch == "\\" and i + 1 < len(dn):
            cur.append(dn[i:i + 2])
            i += 2
            continue
        if ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur or parts:
        parts.append("".join(cur))
    return [p for p in parts if p]


def parse_rules(text: str) -> list[str]:
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def group_cn(dn: str) -> str:
    """'CN=Library-Admins,OU=Groups,DC=x' -> 'Library-Admins'"""
    m = re.match(r"\s*cn\s*=\s*((?:\\.|[^,])+)", dn or "", re.I)
    return re.sub(r"\\(.)", r"\1", m.group(1)).strip() if m else ""


def is_group_name(rule: str) -> bool:
    """A rule without '=' is a group name (e.g. 'Library-Admins')."""
    return "=" not in rule


def rule_matches(rule: str, user_dn: str, group_dns: set[str]) -> bool:
    """Rule = AD group name, group DN or OU/container."""
    rule = (rule or "").strip()
    if not rule:
        return False
    if is_group_name(rule):
        def norm(x):  # same normalization as for DNs (whitespace around commas)
            return re.sub(r"\s*,\s*", ",", x).lower()
        name = norm(rule)
        return any(norm(group_cn(g)) == name for g in group_dns)
    r = normalize_dn(rule)
    u = normalize_dn(user_dn)
    if r in group_dns:
        return True
    # OU/container: the components (RDNs) of the rule must form the end of the user DN. The comparison is
    # component by component – an escaped comma in the name ("CN=Mallory\\, OU=Bib-Admins,…") is not a
    # separator and must not fake membership in an OU.
    rr, ur = split_dn(r), split_dn(u)
    return bool(rr) and len(ur) >= len(rr) and ur[-len(rr):] == rr


def resolve_role(user_dn: str, groups: list[str], cfg: dict | None = None) -> tuple[str | None, str]:
    cfg = cfg or {}
    gset = {normalize_dn(g) for g in groups}
    def rules(key):
        return cfg[key] if key in cfg else get_setting(key)

    checks = (
        (ROLE_ADMIN, rules("ldap_admin_rules")),
        (ROLE_LIBRARIAN, rules("ldap_librarian_rules")),
        (ROLE_READER, rules("ldap_login_rules")),
    )
    for role, text in checks:
        for rule in parse_rules(text):
            if rule_matches(rule, user_dn, gset):
                return role, rule
    return None, ""


def escape_filter(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(value)


class Directory:
    def __init__(self, cfg: dict | None = None):
        def s(key):
            return cfg[key] if cfg and key in cfg else get_setting(key)

        self.cfg = cfg or {}
        self.servers = [x.strip() for x in re.split(r"[,\s]+", s("ldap_server") or "") if x.strip()]
        self.security = s("ldap_security") or "none"
        self.verify = (s("ldap_verify_cert") or "1") in ("1", "true", "on")
        self.ca_file = s("ldap_ca_file") or None
        self.tls_certs = s("ldap_tls_certs") or ""
        self.check_host = (s("ldap_check_hostname") or "1") in ("1", "true", "on")
        self.bind_dn = s("ldap_bind_dn") or ""
        self.bind_password = s("ldap_bind_password") or ""
        self.upn_suffix = (s("ldap_upn_suffix") or "").lstrip("@")
        self.base_dn = s("ldap_base_dn") or ""
        self.user_attr = s("ldap_user_attr") or "sAMAccountName"
        self.user_filter = s("ldap_user_filter") or ""
        self.nested = (s("ldap_nested_groups") or "0") in ("1", "true", "on")
        try:
            self.timeout = int(s("ldap_timeout") or 8)
        except ValueError:
            self.timeout = 8
        if not self.servers:
            raise LDAPError(_("Kein LDAP-Server konfiguriert."))
        if not self.base_dn:
            raise LDAPError(_("Keine Basis-DN konfiguriert."))

    # ---------------------------------------------------------------- Connection
    def _server_pool(self):
        from ldap3 import FIRST, Server, ServerPool

        from .ldap_tls import make_tls

        tls = None
        if self.security in ("starttls", "ldaps") or any(h.lower().startswith("ldaps://") for h in self.servers):
            tls = make_tls(self.tls_certs, self.ca_file, self.verify, self.check_host)
        servers = []
        for host in self.servers:
            use_ssl = self.security == "ldaps" or host.lower().startswith("ldaps://")
            servers.append(Server(host, use_ssl=use_ssl, tls=tls, get_info=None,
                                  connect_timeout=self.timeout))
        return ServerPool(servers, FIRST, active=1, exhaust=True) if len(servers) > 1 else servers[0]

    def connect(self, user: str, password: str):
        from ldap3 import Connection
        from ldap3.core.exceptions import LDAPException

        if not password:
            # Empty password = anonymous bind -> never treat as success
            raise LDAPError(_("Leeres Passwort."))
        conn = None
        try:
            conn = Connection(self._server_pool(), user=user, password=password,
                              receive_timeout=self.timeout, raise_exceptions=False,
                              auto_referrals=False, read_only=True)
            conn.open(read_server_info=False)
            if self.security == "starttls" and not conn.start_tls(read_server_info=False):
                raise LDAPError(_("StartTLS fehlgeschlagen: {error}", error=(conn.result or {}).get('description')))
            if not conn.bind(read_server_info=False):
                desc = conn.result.get("description", "")
                msg = conn.result.get("message", "")
                raise LDAPError(_("Bind abgelehnt ({desc}). {msg}", desc=desc, msg=msg).strip())
            return conn
        except (LDAPException, LDAPError) as exc:
            if conn is not None:
                try:
                    conn.unbind()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(exc, LDAPError):
                raise
            text = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
                raise LDAPError(_("Das Zertifikat des Domänencontrollers ist nicht vertrauenswürdig. "
                                  "Unter „TLS-Zertifikat“ auf „Zertifikat vom DC abrufen“ klicken und bestätigen.")) from exc
            raise LDAPError(_("LDAP-Fehler: {error}", error=exc)) from exc

    def service_connection(self, username: str | None = None, password: str | None = None):
        if self.bind_dn:
            return self.connect(self.bind_dn, self.bind_password)
        if username and password:
            principal = username if ("@" in username or "\\" in username or not self.upn_suffix) \
                else f"{username}@{self.upn_suffix}"
            return self.connect(principal, password)
        raise LDAPError(_("Weder Dienstkonto (Bind-DN) noch UPN-Suffix für direkten Bind konfiguriert."))

    # ---------------------------------------------------------------- Search
    def find_user(self, conn, username: str) -> dict:
        from ldap3 import SUBTREE

        flt = f"({self.user_attr}={escape_filter(username)})"
        if self.user_filter:
            uf = self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})"
            flt = f"(&{uf}{flt})"
        conn.search(self.base_dn, flt, SUBTREE,
                    attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn",
                                "objectSid", "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", "accountExpires", self.user_attr], size_limit=2)
        entries = [e for e in conn.response if e.get("type") == "searchResEntry"]
        if not entries:
            raise LDAPUserNotFound(_("Benutzer '{username}' nicht unterhalb von {base} gefunden.", username=username, base=self.base_dn))
        if len(entries) > 1:
            raise LDAPAmbiguous(_("Benutzername '{username}' ist nicht eindeutig.", username=username))
        return entries[0]

    # ---------------------------------------------------------------- Groups
    def group_base(self, user_dn: str = "") -> str:
        """Search base for groups: the domain root (DC=…), not just the user OU.

        Groups often live outside the user OU (e.g. OU=Groups, CN=Users, CN=Builtin)."""
        for dn in (self.base_dn, user_dn):
            m = re.search(r"(?:^|,)\s*(dc\s*=.*)$", dn or "", re.I)
            if m:
                return m.group(1)
        return self.base_dn

    def _search_dns(self, conn, base: str, flt: str) -> list[str]:
        from ldap3 import SUBTREE
        found = conn.search(base, flt, SUBTREE, attributes=["1.1"])
        self._check_transient(conn)       # also on a partial result (ldap3 still reports True then)
        if not found:
            return []
        return [e["dn"] for e in conn.response if e.get("type") == "searchResEntry"]

    # Transient errors (time/size limit, server busy …). In strict mode (AD sync) such a partial answer must not
    # count as "no groups" – otherwise the person would wrongly be treated as having left. Missing permissions
    # or unsupported matching rules (e.g. OpenLDAP) remain allowed.
    TRANSIENT_RESULTS = {3, 4, 11, 51, 52, 80}
    strict = False

    def _check_transient(self, conn) -> None:
        if not self.strict:
            return
        result = getattr(conn, "result", None) or {}
        code = result.get("result", 0)
        if code in self.TRANSIENT_RESULTS:
            raise LDAPError(_("Verzeichnisabfrage unvollständig ({desc}).", desc=result.get("description") or code))

    def _sids_to_dns(self, conn, base: str, sids: list[bytes]) -> list[str]:
        dns = []
        for i in range(0, len(sids), 40):
            part = "".join(f"(objectSid={_escape_bytes(sid)})" for sid in sids[i:i + 40])
            dns += self._search_dns(conn, base, f"(|{part})")
        return dns

    def user_groups(self, conn, user_dn: str, username: str, entry: dict) -> dict[str, str]:
        """All groups of a user as {DN: origin}.

        Origin: 'direkt' (memberOf), 'primär' (primaryGroupID, e.g. Domain Users –
        NOT listed in memberOf in AD!) or 'verschachtelt' (nested, via other groups).
        Combines several methods so that nothing is lost if one of them is not
        supported by the server or is restricted by permissions."""
        from ldap3 import BASE

        attrs = entry.get("attributes", {}) or {}
        raw = entry.get("raw_attributes", {}) or {}
        base = self.group_base(user_dn)
        found: dict[str, str] = {}

        def add(dns, source):
            for dn in dns:
                key = normalize_dn(dn)
                # do not overwrite a better (more direct) origin
                if key not in {normalize_dn(k) for k in found}:
                    found[dn] = source

        # 1) Direct memberships
        add(_as_list(attrs.get("memberOf")), "direkt")

        # 2) Primary group (AD/Samba: primaryGroupID + the user's domain SID)
        sid = (raw.get("objectSid") or [None])[0]
        pgid = _first(attrs.get("primaryGroupID"))
        primary_dns = []
        if sid and pgid.isdigit() and len(sid) >= 12:
            primary_sid = sid[:-4] + int(pgid).to_bytes(4, "little")
            primary_dns = self._sids_to_dns(conn, base, [primary_sid])
            add(primary_dns, "primär")

        if self.nested:
            # 3) AD/Samba AD: all groups (including nested ones) via LDAP_MATCHING_RULE_IN_CHAIN
            add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(user_dn)})"), "verschachtelt")
            for pdn in primary_dns:  # groups the primary group is a member of
                add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(pdn)})"), "verschachtelt")

            # 4) AD/Samba AD: tokenGroups – exactly the security groups from the logon token
            found_tg = conn.search(user_dn, "(objectClass=*)", BASE, attributes=["tokenGroups"])
            self._check_transient(conn)
            if found_tg:
                for e in conn.response:
                    if e.get("type") == "searchResEntry":
                        sids = (e.get("raw_attributes") or {}).get("tokenGroups") or []
                        if sids:
                            add(self._sids_to_dns(conn, base, list(sids)), "verschachtelt")

        # 5) OpenLDAP / classic Samba domain (no memberOf, no IN_CHAIN rule)
        if not found:
            flt = (f"(|(member={escape_filter(user_dn)})(uniqueMember={escape_filter(user_dn)})"
                   f"(memberUid={escape_filter(username)}))")
            add(self._search_dns(conn, base, flt), "direkt")
            if self.nested:
                queue, seen = list(found), {normalize_dn(d) for d in found}
                for _ in range(10):  # level by level upwards
                    nxt = []
                    for gdn in queue:
                        for parent in self._search_dns(
                                conn, base, f"(|(member={escape_filter(gdn)})(uniqueMember={escape_filter(gdn)}))"):
                            if normalize_dn(parent) not in seen:
                                seen.add(normalize_dn(parent))
                                nxt.append(parent)
                    add(nxt, "verschachtelt")
                    if not nxt:
                        break
                    queue = nxt
        return found

    def list_groups(self, query: str = "", limit: int = 1000) -> list[dict]:
        """Groups from the directory for selection in the UI."""
        from ldap3 import SUBTREE

        flt = ("(|(objectClass=group)(objectClass=groupOfNames)"
               "(objectClass=groupOfUniqueNames)(objectClass=posixGroup))")
        if query:
            q = escape_filter(query.strip())
            flt = f"(&{flt}(|(cn=*{q}*)(description=*{q}*)))"
        conn = self.service_connection()
        try:
            entries = conn.extend.standard.paged_search(self.group_base(), flt, SUBTREE,
                                                        attributes=["cn", "description"],
                                                        paged_size=500, generator=True)
            groups = []
            for e in entries:
                if e.get("type") != "searchResEntry":
                    continue
                attrs = e.get("attributes", {})
                groups.append({"name": _first(attrs.get("cn")) or group_cn(e["dn"]), "dn": e["dn"],
                               "description": _first(attrs.get("description"))})
                if len(groups) >= limit:
                    break
        finally:
            conn.unbind()
        return sorted(groups, key=lambda g: g["name"].lower())

    # ---------------------------------------------------------------- Login
    def authenticate(self, username: str, password: str) -> LDAPUser:
        username = (username or "").strip()
        if "\\" in username:  # DOMAIN\user
            username = username.split("\\", 1)[1]
        if "@" in username and self.user_attr.lower() == "samaccountname":
            username = username.split("@", 1)[0]
        if not username or not password:
            raise LDAPError(_("Benutzername oder Passwort fehlt."))

        svc = self.service_connection(username, password)
        user_conn = None
        try:
            entry = self.find_user(svc, username)
            user_dn = entry["dn"]
            attrs = entry.get("attributes", {})
            # Check the password against EXACTLY this entry – also without a service account: there the first bind only
            # confirmed "name@UPN suffix", and in AD the UPN and the logon name can belong to different people.
            user_conn = self.connect(user_dn, password)
            # Prefer reading the groups as the user themselves: everyone may read their own
            # tokenGroups, a service account in hardened domains not always.
            group_info = self.user_groups(user_conn or svc, user_dn, username, entry)
            if not group_info and user_conn is not None:
                group_info = self.user_groups(svc, user_dn, username, entry)
        finally:
            for c in (user_conn, svc):
                try:
                    if c is not None:
                        c.unbind()
                except Exception:
                    pass
        groups = list(group_info)

        display = _first(attrs.get("displayName")) or " ".join(
            x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x
        ) or _first(attrs.get("cn")) or username
        role, rule = resolve_role(user_dn, groups, self.cfg)
        return LDAPUser(username=_first(attrs.get(self.user_attr)) or username, dn=user_dn,
                        display_name=display, email=_first(attrs.get("mail")) or "",
                        groups=groups, role=role, matched_rule=rule, group_info=group_info, guid=entry_guid(entry))

    def _user_from_entry(self, conn, entry: dict, username: str, cached_groups: bool = False) -> "LDAPUser | None":
        """Directory entry → LDAPUser with groups and role; None for an account disabled in AD.

        cached_groups=True (suggestion search only): reuse groups for up to SEARCH_GROUP_TTL seconds."""
        attrs = entry.get("attributes", {})
        if _disabled(attrs):   # disabled or expired in AD
            return None
        user_dn = entry["dn"]
        name = _first(attrs.get(self.user_attr)) or username
        if cached_groups:
            import time
            # Direct groups and the primary group are part of the key: if they change in AD, groups are resolved again
            key = "|".join([normalize_dn(user_dn), _first(attrs.get("primaryGroupID")),
                            ";".join(sorted(normalize_dn(str(g)) for g in _as_list(attrs.get("memberOf"))))])
            hit = _search_groups.get(key)
            if hit and time.monotonic() - hit[0] < SEARCH_GROUP_TTL:
                group_info = hit[1]
            else:
                group_info = self.user_groups(conn, user_dn, name, entry)
                if len(_search_groups) > 2000:
                    _search_groups.clear()
                _search_groups[key] = (time.monotonic(), group_info)
        else:
            group_info = self.user_groups(conn, user_dn, name, entry)
        groups = list(group_info)
        display = _first(attrs.get("displayName")) or " ".join(
            x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x) or _first(attrs.get("cn")) or name
        role, rule = resolve_role(user_dn, groups, self.cfg)
        return LDAPUser(username=name, dn=user_dn, display_name=display, email=_first(attrs.get("mail")) or "",
                        groups=groups, role=role, matched_rule=rule, group_info=group_info, guid=entry_guid(entry))

    def check_account(self, username: str) -> "LDAPUser | None":
        """Does the account still exist, is it active and entitled? (for active sessions, without a password)

        Returns None if the account is missing or disabled in AD. Connection problems raise
        LDAPError – then the session is kept (an AD outage must not lock anyone out)."""
        if not self.bind_dn:
            raise LDAPError(_("Ohne Dienstkonto kann eine Sitzung nicht nachgeprüft werden."))
        svc = self.service_connection()
        try:
            try:
                entry = self.find_user(svc, username)
            except LDAPUserNotFound:
                return None
            return self._user_from_entry(svc, entry, username)
        finally:
            try:
                svc.unbind()
            except Exception:  # noqa: BLE001
                pass

    def search_users(self, query: str, limit: int = 10) -> list["LDAPUser"]:
        """Search for people in the directory (name, username, email) – only with a service account.

        Only active accounts that may use Catalytix according to the role rules are returned. Nobody is
        created or logged in by this."""
        from ldap3 import SUBTREE

        query = (query or "").strip()
        if not self.bind_dn:
            raise LDAPError(_("Für die Suche im Verzeichnis ist ein Dienstkonto nötig."))
        if len(query) < 2:
            return []
        q = escape_filter(query)
        attrs_or = "".join(f"({a}=*{q}*)" for a in (self.user_attr, "displayName", "cn", "mail", "givenName", "sn"))
        flt = f"(|{attrs_or})"
        if self.user_attr.lower() == "samaccountname":
            # Active Directory only: exclude disabled accounts right in the filter (OpenLDAP does not know this –
            # there the expression would discard all hits). _user_from_entry checks again anyway.
            flt = f"(&{flt}(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
        if self.user_filter:
            uf = self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})"
            flt = f"(&{uf}{flt})"
        svc = self.service_connection()
        found: list[LDAPUser] = []
        try:
            svc.search(self.base_dn, flt, SUBTREE, size_limit=limit * 2,
                       attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid",
                                   "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", "accountExpires", self.user_attr])
            entries = [e for e in svc.response if e.get("type") == "searchResEntry"]
            for entry in entries:
                info = self._user_from_entry(svc, entry, "", cached_groups=True)
                if info is not None and info.role is not None and info.username:
                    found.append(info)
                if len(found) >= limit:
                    break
        finally:
            try:
                svc.unbind()
            except Exception:  # noqa: BLE001
                pass
        return sorted(found, key=lambda u: (u.display_name or u.username).lower())

    def scan_people(self, reuse=None, page_size: int = 500, progress=None):
        """All active people who may use Catalytix according to the role rules – for the search cache.

        reuse(dn, group_key) returns still-valid group DNs from the cache (or None → resolve again).
        Result: list of (LDAPUser, group_key, newly_resolved). If the search is incomplete (size limit,
        abort), LDAPError is raised – then nobody may be removed from the cache."""
        import hashlib

        from ldap3 import SUBTREE

        if not self.bind_dn:
            raise LDAPError(_("Für den Abgleich ist ein Dienstkonto nötig."))
        parts = [f"({self.user_attr}=*)"]
        if self.user_filter:
            parts.append(self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})")
        if self.user_attr.lower() == "samaccountname":
            # only real people (no computer accounts), leave out disabled ones right away
            parts += ["(objectCategory=person)", "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"]
        flt = f"(&{''.join(parts)})"
        attrs = ["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid", "primaryGroupID",
                 "objectGUID", "entryUUID", "userAccountControl", "accountExpires", self.user_attr]
        conn = self.service_connection()
        try:
            # First read all pages completely and check the result – the group resolution afterwards uses
            # the same connection and overwrites conn.result.
            entries = [e for e in conn.extend.standard.paged_search(self.base_dn, flt, SUBTREE, attributes=attrs,
                                                                     paged_size=page_size, generator=True)
                       if e.get("type") == "searchResEntry"]
            res = conn.result or {}
            if res.get("result", 0) != 0:
                raise LDAPError(_("Abgleich unvollständig ({desc}) – der Cache bleibt unverändert.",
                                  desc=res.get("description") or res.get("result")))
            found = ScanResult()
            self.strict = True
            for n, entry in enumerate(entries):
                if progress and n % 50 == 0:
                    progress(n, len(entries))
                a = entry.get("attributes", {}) or {}
                key = hashlib.sha256("|".join([
                    normalize_dn(entry["dn"]), _first(a.get("primaryGroupID")),
                    ";".join(sorted(normalize_dn(str(g)) for g in _as_list(a.get("memberOf"))))]).encode()).hexdigest()
                groups = reuse(entry["dn"], key) if reuse else None
                fresh = groups is None
                try:
                    if fresh:
                        info = self._user_from_entry(conn, entry, "")
                    else:
                        info = self._user_from_known_groups(entry, groups)
                except LDAPError as exc:
                    # Groups not reliably determinable: the person exists, but their status is unknown – do not remove
                    log.warning("AD sync: groups of %s cannot be determined: %s", entry.get("dn"), exc)
                    found.uncertain.append(((_first(a.get(self.user_attr)) or "").lower(), entry_guid(entry)))
                    continue
                if info is not None and info.role is not None and info.username:
                    found.append((info, key, fresh))
            if len(found.uncertain) > max(5, len(entries) // 5):
                raise LDAPError(_("Abgleich unvollständig: bei {n} Personen waren die Gruppen nicht ermittelbar – "
                                  "der Cache bleibt unverändert.", n=len(found.uncertain)))
            return found
        finally:
            self.strict = False
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass

    def find_by_guid(self, guid: str) -> "LDAPUser | None":
        """Look up a person by the stable identifier (e.g. to find out whether they were only renamed).
        None if they do not exist (any more) in AD or are disabled."""
        if not guid or not self.bind_dn:
            return None
        from ldap3 import SUBTREE
        try:
            raw = bytes.fromhex(guid)
        except ValueError:
            return None
        if len(raw) != 16:
            return None
        # Stored is either the objectGUID (AD) or the entryUUID (OpenLDAP) – both 16 bytes. Query both:
        # the attribute unknown to the server simply yields no hit in the filter.
        uuid = f"{guid[:8]}-{guid[8:12]}-{guid[12:16]}-{guid[16:20]}-{guid[20:32]}"
        flt = f"(|(objectGUID={_escape_bytes(raw)})(entryUUID={uuid}))"
        svc = self.service_connection()
        try:
            svc.search(self.base_dn, flt, SUBTREE, size_limit=2,
                       attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid",
                                   "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", "accountExpires", self.user_attr])
            entries = [e for e in svc.response if e.get("type") == "searchResEntry"]
            if not entries:
                code = (svc.result or {}).get("result", 0)
                if code not in (0, 32):
                    raise LDAPError(_("Verzeichnisabfrage fehlgeschlagen ({desc}).",
                                      desc=(svc.result or {}).get("description") or code))
                return None
            return self._user_from_entry(svc, entries[0], "")
        finally:
            try:
                svc.unbind()
            except Exception:  # noqa: BLE001
                pass

    def _user_from_known_groups(self, entry: dict, groups: list[str]) -> "LDAPUser | None":
        """Like _user_from_entry, but with already known groups (no further queries)."""
        attrs = entry.get("attributes", {}) or {}
        if _disabled(attrs):
            return None
        name = _first(attrs.get(self.user_attr))
        display = _first(attrs.get("displayName")) or " ".join(
            x for x in (_first(attrs.get("givenName")), _first(attrs.get("sn"))) if x) or _first(attrs.get("cn")) or name
        role, rule = resolve_role(entry["dn"], list(groups), self.cfg)
        return LDAPUser(username=name, dn=entry["dn"], display_name=display, email=_first(attrs.get("mail")) or "",
                        groups=list(groups), role=role, matched_rule=rule, guid=entry_guid(entry))

    def test_connection(self) -> str:
        conn = self.service_connection()
        who = conn.extend.standard.who_am_i() if hasattr(conn, "extend") else ""
        ok = conn.search(self.base_dn, "(objectClass=*)", "BASE", attributes=["objectClass"])
        conn.unbind()
        if not ok:
            raise LDAPError(_("Bind erfolgreich, aber Basis-DN '{base}' nicht lesbar.", base=self.base_dn))
        return _("Verbindung erfolgreich. Angemeldet als {who}. Basis-DN ist erreichbar.", who=who or self.bind_dn)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return v
    return [v]


def _escape_bytes(data: bytes) -> str:
    """Encode a binary value (e.g. a SID) for an LDAP filter."""
    return "".join(f"\\{b:02x}" for b in data)


AD_NEVER = 0x7FFFFFFFFFFFFFFF


def _expired(value) -> bool:
    """accountExpires (AD): expired? 0 and 0x7FFF… mean "never". Depending on schema info, ldap3 returns a number
    (100 ns steps since 1601) or already a datetime."""
    from datetime import datetime, timedelta, timezone
    vals = _as_list(value)
    if not vals:
        return False
    v = vals[0]
    if isinstance(v, datetime):
        if v.year <= 1601 or v.year >= 9999:
            return False
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v <= datetime.now(timezone.utc)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return False
    if n <= 0 or n >= AD_NEVER:
        return False
    return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=n // 10) <= datetime.now(timezone.utc)


def _disabled(attrs: dict) -> bool:
    """Disabled in AD (userAccountControl, bit 2) or expired (accountExpires)."""
    try:
        uac = int(_first(attrs.get("userAccountControl")) or 0)
    except (TypeError, ValueError):
        uac = 0
    return bool(uac & 2) or _expired(attrs.get("accountExpires"))


def _first(v) -> str:
    lst = _as_list(v)
    return str(lst[0]) if lst else ""


def ldap_enabled() -> bool:
    return get_bool("ldap_enabled")


def authenticate(username: str, password: str) -> LDAPUser:
    return Directory().authenticate(username, password)


__all__ = ["LDAPError", "LDAPUser", "Directory", "authenticate", "ldap_enabled",
           "resolve_role", "rule_matches", "group_cn", "is_group_name", "normalize_dn", "parse_rules", "get_int"]
