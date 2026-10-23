"""Anmeldung gegen Active Directory (Windows Server oder Samba AD) bzw. LDAP.

Ablauf
  1. Verbindung mit dem Dienstkonto (Bind-DN) – oder, falls keines hinterlegt ist,
     direkt als Benutzer per UPN (benutzer@domaene.local).
  2. Suche des Benutzers unterhalb der Basis-DN über das Login-Attribut
     (AD/Samba-AD: sAMAccountName, OpenLDAP/klassisches Samba: uid).
  3. Bind mit dem DN und Passwort des Benutzers (eigentliche Passwortprüfung).
  4. Ermitteln der Gruppen (memberOf, optional verschachtelt über
     LDAP_MATCHING_RULE_IN_CHAIN, Fallback member/uniqueMember/memberUid).
  5. Regeln anwenden. Jede Regel ist entweder eine OU/ein Container
     (der Benutzer liegt darunter) oder der DN einer Gruppe (der Benutzer ist Mitglied):
       - Admin-Regeln       -> Rolle Administrator
       - Verwalter-Regeln   -> Rolle Verwalter
       - Login-Regeln       -> Rolle Leser
     Ist keine Regel erfüllt, wird die Anmeldung abgelehnt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .i18n import _
from .models import ROLE_ADMIN, ROLE_LIBRARIAN, ROLE_READER, get_bool, get_int, get_setting

# Gruppen von Personen aus der Vorschlagssuche kurz merken: Beim Weitertippen („ann“ → „anna“) kommen dieselben
# Personen wieder, die verschachtelte Auflösung kostet aber mehrere Abfragen je Person. Nur für die Suche –
# Anmeldung und Nachprüfung fragen immer frisch.
SEARCH_GROUP_TTL = 300
_search_groups: dict[str, tuple[float, dict]] = {}

log = logging.getLogger(__name__)
IN_CHAIN = "1.2.840.113556.1.4.1941"


class LDAPError(Exception):
    """Fehler mit einer für Administratoren verständlichen Meldung."""


class LDAPUserNotFound(LDAPError):
    """Das Konto existiert (nicht mehr) im Verzeichnis."""


class LDAPAmbiguous(LDAPError):
    """Der Benutzername passt auf mehrere Einträge im Verzeichnis."""


@dataclass
class LDAPUser:
    username: str
    dn: str
    display_name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)
    role: str | None = None
    matched_rule: str = ""
    group_info: dict = field(default_factory=dict)  # DN -> direkt | primär | verschachtelt
    guid: str = ""      # objectGUID (AD) bzw. entryUUID – bleibt bei Umbenennung und Verschieben gleich


class ScanResult(list):
    """Ergebnis von scan_people: (LDAPUser, group_key, neu_aufgelöst) je Person, dazu .uncertain:
    (benutzername, guid) der Personen, deren Gruppen nicht sicher ermittelbar waren."""

    def __init__(self, *a):
        super().__init__(*a)
        self.uncertain: list[tuple[str, str]] = []


def entry_guid(entry: dict) -> str:
    """Feste Kennung eines Eintrags: objectGUID (AD) oder entryUUID (OpenLDAP), sonst leer."""
    raw = (entry.get("raw_attributes") or {})
    g = raw.get("objectGUID") or []
    if g and isinstance(g[0], (bytes, bytearray)) and len(g[0]) == 16:
        return g[0].hex()
    attrs = entry.get("attributes") or {}
    u = _first(attrs.get("entryUUID"))
    return u.replace("-", "").lower() if u else ""


def normalize_dn(dn: str) -> str:
    return re.sub(r"\s*([,=])\s*", r"\1", (dn or "").strip()).lower()


def parse_rules(text: str) -> list[str]:
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def group_cn(dn: str) -> str:
    """'CN=Bibliothek-Admins,OU=Gruppen,DC=x' -> 'Bibliothek-Admins'"""
    m = re.match(r"\s*cn\s*=\s*((?:\\.|[^,])+)", dn or "", re.I)
    return re.sub(r"\\(.)", r"\1", m.group(1)).strip() if m else ""


def is_group_name(rule: str) -> bool:
    """Eine Regel ohne '=' ist ein Gruppenname (z. B. 'Bibliothek-Admins')."""
    return "=" not in rule


def rule_matches(rule: str, user_dn: str, group_dns: set[str]) -> bool:
    """Regel = AD-Gruppenname, Gruppen-DN oder OU/Container."""
    rule = (rule or "").strip()
    if not rule:
        return False
    if is_group_name(rule):
        def norm(x):  # gleiche Normalisierung wie bei DNs (Leerzeichen um Kommas)
            return re.sub(r"\s*,\s*", ",", x).lower()
        name = norm(rule)
        return any(norm(group_cn(g)) == name for g in group_dns)
    r = normalize_dn(rule)
    u = normalize_dn(user_dn)
    if r in group_dns:
        return True
    return u == r or u.endswith("," + r)


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

    # ---------------------------------------------------------------- Verbindung
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
            # Leeres Passwort = anonymer Bind -> niemals als Erfolg werten
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

    # ---------------------------------------------------------------- Suche
    def find_user(self, conn, username: str) -> dict:
        from ldap3 import SUBTREE

        flt = f"({self.user_attr}={escape_filter(username)})"
        if self.user_filter:
            uf = self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})"
            flt = f"(&{uf}{flt})"
        conn.search(self.base_dn, flt, SUBTREE,
                    attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn",
                                "objectSid", "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", self.user_attr], size_limit=2)
        entries = [e for e in conn.response if e.get("type") == "searchResEntry"]
        if not entries:
            raise LDAPUserNotFound(_("Benutzer '{username}' nicht unterhalb von {base} gefunden.", username=username, base=self.base_dn))
        if len(entries) > 1:
            raise LDAPAmbiguous(_("Benutzername '{username}' ist nicht eindeutig.", username=username))
        return entries[0]

    # ---------------------------------------------------------------- Gruppen
    def group_base(self, user_dn: str = "") -> str:
        """Suchbasis für Gruppen: die Domänenwurzel (DC=…), nicht nur die Benutzer-OU.

        Gruppen liegen oft außerhalb der Benutzer-OU (z. B. OU=Gruppen, CN=Users, CN=Builtin)."""
        for dn in (self.base_dn, user_dn):
            m = re.search(r"(?:^|,)\s*(dc\s*=.*)$", dn or "", re.I)
            if m:
                return m.group(1)
        return self.base_dn

    def _search_dns(self, conn, base: str, flt: str) -> list[str]:
        from ldap3 import SUBTREE
        found = conn.search(base, flt, SUBTREE, attributes=["1.1"])
        self._check_transient(conn)       # auch bei Teilergebnis (ldap3 meldet dann trotzdem True)
        if not found:
            return []
        return [e["dn"] for e in conn.response if e.get("type") == "searchResEntry"]

    # Vorübergehende Fehler (Zeit-/Größenlimit, Server beschäftigt …). Im strengen Modus (AD-Abgleich) darf eine
    # solche Teilantwort nicht als „keine Gruppen“ gelten – sonst würde die Person fälschlich als ausgeschieden
    # behandelt. Fehlende Rechte oder nicht unterstützte Suchregeln (z. B. OpenLDAP) bleiben erlaubt.
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
        """Alle Gruppen eines Benutzers als {DN: Herkunft}.

        Herkunft: 'direkt' (memberOf), 'primär' (primaryGroupID, z. B. Domänen-Benutzer –
        steht in AD NICHT in memberOf!) oder 'verschachtelt' (über andere Gruppen).
        Kombiniert mehrere Verfahren, damit nichts verloren geht, wenn eines davon
        vom Server nicht unterstützt oder durch Rechte eingeschränkt wird."""
        from ldap3 import BASE

        attrs = entry.get("attributes", {}) or {}
        raw = entry.get("raw_attributes", {}) or {}
        base = self.group_base(user_dn)
        found: dict[str, str] = {}

        def add(dns, source):
            for dn in dns:
                key = normalize_dn(dn)
                # bessere (direktere) Herkunft nicht überschreiben
                if key not in {normalize_dn(k) for k in found}:
                    found[dn] = source

        # 1) Direkte Mitgliedschaften
        add(_as_list(attrs.get("memberOf")), "direkt")

        # 2) Primäre Gruppe (AD/Samba: primaryGroupID + Domänen-SID des Benutzers)
        sid = (raw.get("objectSid") or [None])[0]
        pgid = _first(attrs.get("primaryGroupID"))
        primary_dns = []
        if sid and pgid.isdigit() and len(sid) >= 12:
            primary_sid = sid[:-4] + int(pgid).to_bytes(4, "little")
            primary_dns = self._sids_to_dns(conn, base, [primary_sid])
            add(primary_dns, "primär")

        if self.nested:
            # 3) AD/Samba-AD: alle (auch verschachtelten) Gruppen über LDAP_MATCHING_RULE_IN_CHAIN
            add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(user_dn)})"), "verschachtelt")
            for pdn in primary_dns:  # Gruppen, in denen die primäre Gruppe Mitglied ist
                add(self._search_dns(conn, base, f"(member:{IN_CHAIN}:={escape_filter(pdn)})"), "verschachtelt")

            # 4) AD/Samba-AD: tokenGroups – exakt die Sicherheitsgruppen aus dem Anmeldetoken
            found_tg = conn.search(user_dn, "(objectClass=*)", BASE, attributes=["tokenGroups"])
            self._check_transient(conn)
            if found_tg:
                for e in conn.response:
                    if e.get("type") == "searchResEntry":
                        sids = (e.get("raw_attributes") or {}).get("tokenGroups") or []
                        if sids:
                            add(self._sids_to_dns(conn, base, list(sids)), "verschachtelt")

        # 5) OpenLDAP / klassische Samba-Domäne (kein memberOf, keine IN_CHAIN-Regel)
        if not found:
            flt = (f"(|(member={escape_filter(user_dn)})(uniqueMember={escape_filter(user_dn)})"
                   f"(memberUid={escape_filter(username)}))")
            add(self._search_dns(conn, base, flt), "direkt")
            if self.nested:
                queue, seen = list(found), {normalize_dn(d) for d in found}
                for _ in range(10):  # Ebene für Ebene nach oben
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
        """Gruppen aus dem Verzeichnis zur Auswahl in der Oberfläche."""
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
        if "\\" in username:  # DOMAENE\benutzer
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
            # Passwort gegen GENAU diesen Eintrag prüfen – auch ohne Dienstkonto: Dort hat der erste Bind nur
            # „name@UPN-Suffix“ bestätigt, und UPN und Anmeldename können im AD zu verschiedenen Personen gehören.
            user_conn = self.connect(user_dn, password)
            # Gruppen bevorzugt als der Benutzer selbst lesen: jeder darf sein eigenes
            # tokenGroups lesen, ein Dienstkonto in gehärteten Domänen nicht immer.
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
        """Verzeichniseintrag → LDAPUser mit Gruppen und Rolle; None bei im AD deaktiviertem Konto.

        cached_groups=True (nur Vorschlagssuche): Gruppen bis zu SEARCH_GROUP_TTL Sekunden wiederverwenden."""
        attrs = entry.get("attributes", {})
        try:
            uac = int(_first(attrs.get("userAccountControl")) or 0)
        except (TypeError, ValueError):
            uac = 0
        if uac & 2:   # ACCOUNTDISABLE
            return None
        user_dn = entry["dn"]
        name = _first(attrs.get(self.user_attr)) or username
        if cached_groups:
            import time
            # Direkte Gruppen und primäre Gruppe gehören zum Schlüssel: ändern sie sich im AD, wird neu aufgelöst
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
        """Ist das Konto noch vorhanden, aktiv und berechtigt? (für laufende Sitzungen, ohne Passwort)

        Liefert None, wenn das Konto fehlt oder im AD deaktiviert ist. Verbindungsprobleme lösen
        LDAPError aus – dann bleibt die Sitzung bestehen (ein AD-Ausfall soll niemanden aussperren)."""
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
        """Personen im Verzeichnis suchen (Name, Benutzername, E-Mail) – nur mit Dienstkonto.

        Geliefert werden nur aktive Konten, die laut Rollenregeln Catalytix nutzen dürfen. Angelegt oder
        angemeldet wird dabei niemand."""
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
            # Nur im Active Directory: deaktivierte Konten gleich im Filter ausschließen (OpenLDAP kennt das nicht –
            # dort würde der Ausdruck alle Treffer verwerfen). _user_from_entry prüft ohnehin noch einmal.
            flt = f"(&{flt}(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
        if self.user_filter:
            uf = self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})"
            flt = f"(&{uf}{flt})"
        svc = self.service_connection()
        found: list[LDAPUser] = []
        try:
            svc.search(self.base_dn, flt, SUBTREE, size_limit=limit * 2,
                       attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid",
                                   "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", self.user_attr])
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
        """Alle aktiven Personen, die laut Rollenregeln Catalytix nutzen dürfen – für den Such-Cache.

        reuse(dn, group_key) liefert noch gültige Gruppen-DNs aus dem Cache (oder None → neu auflösen).
        Ergebnis: Liste von (LDAPUser, group_key, neu_aufgelöst). Ist die Suche unvollständig (Größenlimit,
        Abbruch), wird LDAPError ausgelöst – dann darf niemand aus dem Cache gelöscht werden."""
        import hashlib

        from ldap3 import SUBTREE

        if not self.bind_dn:
            raise LDAPError(_("Für den Abgleich ist ein Dienstkonto nötig."))
        parts = [f"({self.user_attr}=*)"]
        if self.user_filter:
            parts.append(self.user_filter if self.user_filter.startswith("(") else f"({self.user_filter})")
        if self.user_attr.lower() == "samaccountname":
            # nur echte Personen (keine Computerkonten), deaktivierte gleich weglassen
            parts += ["(objectCategory=person)", "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"]
        flt = f"(&{''.join(parts)})"
        attrs = ["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid", "primaryGroupID",
                 "objectGUID", "entryUUID", "userAccountControl", self.user_attr]
        conn = self.service_connection()
        try:
            # Erst alle Seiten vollständig lesen und das Ergebnis prüfen – die Gruppenauflösung danach nutzt
            # dieselbe Verbindung und überschreibt conn.result.
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
                    # Gruppen nicht sicher ermittelbar: die Person ist da, aber ihr Stand ungewiss – nicht entfernen
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
        """Person über die feste Kennung suchen (z. B. um festzustellen, ob sie nur umbenannt wurde).
        None, wenn es sie im AD nicht (mehr) gibt oder sie deaktiviert ist."""
        if not guid or not self.bind_dn:
            return None
        from ldap3 import SUBTREE
        try:
            raw = bytes.fromhex(guid)
        except ValueError:
            return None
        if len(raw) != 16:
            return None
        # Gespeichert ist entweder die objectGUID (AD) oder die entryUUID (OpenLDAP) – beide 16 Byte. Beides fragen:
        # das jeweils unbekannte Attribut ergibt im Filter einfach keinen Treffer.
        uuid = f"{guid[:8]}-{guid[8:12]}-{guid[12:16]}-{guid[16:20]}-{guid[20:32]}"
        flt = f"(|(objectGUID={_escape_bytes(raw)})(entryUUID={uuid}))"
        svc = self.service_connection()
        try:
            svc.search(self.base_dn, flt, SUBTREE, size_limit=2,
                       attributes=["displayName", "cn", "mail", "memberOf", "givenName", "sn", "objectSid",
                                   "primaryGroupID", "objectGUID", "entryUUID", "userAccountControl", self.user_attr])
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
        """Wie _user_from_entry, aber mit schon bekannten Gruppen (ohne weitere Abfragen)."""
        attrs = entry.get("attributes", {}) or {}
        try:
            uac = int(_first(attrs.get("userAccountControl")) or 0)
        except (TypeError, ValueError):
            uac = 0
        if uac & 2:
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
    """Binärwert (z. B. SID) für einen LDAP-Filter kodieren."""
    return "".join(f"\\{b:02x}" for b in data)


def _first(v) -> str:
    lst = _as_list(v)
    return str(lst[0]) if lst else ""


def ldap_enabled() -> bool:
    return get_bool("ldap_enabled")


def authenticate(username: str, password: str) -> LDAPUser:
    return Directory().authenticate(username, password)


__all__ = ["LDAPError", "LDAPUser", "Directory", "authenticate", "ldap_enabled",
           "resolve_role", "rule_matches", "group_cn", "is_group_name", "normalize_dn", "parse_rules", "get_int"]
