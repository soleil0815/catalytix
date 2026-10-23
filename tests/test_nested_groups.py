"""Tests der Gruppenauflösung (verschachtelte Gruppen, primäre Gruppe, OpenLDAP).

Simuliert ein Verzeichnis mit den Eigenheiten von Active Directory / Samba AD:
  * memberOf enthält nur direkte Mitgliedschaften – NICHT die primäre Gruppe
  * die primäre Gruppe (Domänen-Benutzer) steht nicht in deren member-Attribut,
    sondern nur über primaryGroupID am Benutzer
  * LDAP_MATCHING_RULE_IN_CHAIN liefert transitive Mitgliedschaften (über member)
  * tokenGroups (am Benutzer, Scope BASE) enthält alle Sicherheitsgruppen inkl. primärer
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-nested-"))

from catalytix import ldap_auth  # noqa: E402
from catalytix.ldap_auth import IN_CHAIN, normalize_dn  # noqa: E402

DOMAIN_SID = bytes([1, 4, 0, 0, 0, 0, 0, 5]) + (21).to_bytes(4, "little") + (111).to_bytes(4, "little") \
    + (222).to_bytes(4, "little") + (333).to_bytes(4, "little")


def sid(rid):
    s = bytearray(DOMAIN_SID)
    s[1] = 5  # 5 Unterautoritäten
    return bytes(s) + rid.to_bytes(4, "little")


def unescape_filter_value(v):
    return re.sub(r"\\([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), v)


class FakeDirectory:
    """Sehr kleiner LDAP-Nachbau – nur die Filter, die CATALYTIX verwendet."""

    def __init__(self, entries, in_chain=True, token_groups=True, member_of=True):
        self.entries = entries  # dn -> dict
        self.in_chain = in_chain
        self.token_groups = token_groups
        self.member_of = member_of
        self.response = []
        self.queries = []

    def _under(self, dn, base):
        d, b = normalize_dn(dn), normalize_dn(base)
        return d == b or d.endswith("," + b)

    def _groups_with_member(self, dn, attrs=("member",)):
        return [g for g, e in self.entries.items()
                if any(normalize_dn(dn) in {normalize_dn(m) for m in e.get(a, [])} for a in attrs)]

    def _transitive(self, dn):
        result, queue = [], [dn]
        seen = set()
        while queue:
            cur = queue.pop()
            for g in self._groups_with_member(cur):
                if normalize_dn(g) not in seen:
                    seen.add(normalize_dn(g))
                    result.append(g)
                    queue.append(g)
        return result

    def _entry(self, dn, raw=None):
        return {"type": "searchResEntry", "dn": dn, "attributes": {}, "raw_attributes": raw or {}}

    def search(self, base, flt, scope, attributes=None, size_limit=0):
        self.queries.append(flt)
        self.response = []
        if scope == "BASE" and "tokenGroups" in (attributes or []):
            if not self.token_groups:
                return False
            user = self.entries[base]
            sids = [sid(user["primaryGroupID"])]
            seen = set()
            for g in self._transitive(base) + self._transitive(self._by_sid(sid(user["primaryGroupID"]))):
                if normalize_dn(g) not in seen and self.entries[g].get("security", True):
                    seen.add(normalize_dn(g))
                    sids.append(self.entries[g]["objectSid"])
            self.response = [self._entry(base, {"tokenGroups": sids})]
            return True
        m = re.fullmatch(rf"\(member:{re.escape(IN_CHAIN)}:=(.+)\)", flt)
        if m:
            if not self.in_chain:
                return False
            hits = self._transitive(unescape_filter_value(m.group(1)))
        elif flt.startswith("(|(objectSid="):
            wanted = [bytes(int(h, 16) for h in re.findall(r"\\([0-9a-f]{2})", part))
                      for part in re.findall(r"\(objectSid=([^)]*)\)", flt)]
            hits = [dn for dn, e in self.entries.items() if e.get("objectSid") in wanted]
        elif flt.startswith("(|(member="):
            vals = [unescape_filter_value(v) for v in re.findall(r"\((?:member|uniqueMember|memberUid)=([^)]*)\)", flt)]
            hits = [g for g, e in self.entries.items()
                    if any(normalize_dn(v) in {normalize_dn(x) for x in e.get("member", []) + e.get("uniqueMember", [])}
                           or v in e.get("memberUid", []) for v in vals)]
        else:
            raise AssertionError(f"Unerwarteter Filter: {flt}")
        hits = [h for h in hits if self._under(h, base)]
        self.response = [self._entry(h) for h in hits]
        return bool(hits)

    def unbind(self):
        pass

    def _by_sid(self, s):
        return next(dn for dn, e in self.entries.items() if e.get("objectSid") == s)

    def user_entry(self, dn):
        e = self.entries[dn]
        attrs = {"memberOf": [g for g, ge in self.entries.items() if dn in ge.get("member", [])]
                 if self.member_of else [], "primaryGroupID": str(e.get("primaryGroupID", ""))}
        return {"dn": dn, "attributes": attrs, "raw_attributes": {"objectSid": [e["objectSid"]]}}


BASE = "DC=schule,DC=local"
LENA = "CN=Lena Lehrer,OU=Lehrer,OU=Benutzer,DC=schule,DC=local"
MAX = "CN=Max Schueler,OU=Schueler,OU=Benutzer,DC=schule,DC=local"
G = {
    "dombenutzer": "CN=Domänen-Benutzer,CN=Users,DC=schule,DC=local",
    "lehrer": "CN=Lehrer,OU=Gruppen,DC=schule,DC=local",
    "verwalter": "CN=Bibliothek-Verwalter,OU=Gruppen,DC=schule,DC=local",
    "leser": "CN=Bibliothek-Leser,OU=Gruppen,DC=schule,DC=local",
    "admins": "CN=Bibliothek-Admins,OU=Gruppen,DC=schule,DC=local",
    "ring1": "CN=Ring1,OU=Gruppen,DC=schule,DC=local",
    "ring2": "CN=Ring2,OU=Gruppen,DC=schule,DC=local",
}


def ad_entries():
    return {
        LENA: {"objectSid": sid(1101), "primaryGroupID": 513},
        MAX: {"objectSid": sid(1102), "primaryGroupID": 513},
        G["dombenutzer"]: {"objectSid": sid(513), "member": []},          # primäre Gruppe: member leer!
        G["lehrer"]: {"objectSid": sid(2001), "member": [LENA]},
        G["verwalter"]: {"objectSid": sid(2002), "member": [G["lehrer"]]},   # verschachtelt: Lehrer
        G["leser"]: {"objectSid": sid(2003), "member": [G["dombenutzer"]]},  # verschachtelt über primäre Gruppe
        G["admins"]: {"objectSid": sid(2004), "member": []},
        G["ring1"]: {"objectSid": sid(2005), "member": [MAX, G["ring2"]]},   # Zirkelbezug
        G["ring2"]: {"objectSid": sid(2006), "member": [G["ring1"]]},
    }


def make_dir(nested=True, base_dn="OU=Benutzer,DC=schule,DC=local"):
    return ldap_auth.Directory({
        "ldap_server": "ldap://dc1", "ldap_security": "none", "ldap_verify_cert": "1", "ldap_ca_file": "",
        "ldap_tls_certs": "", "ldap_check_hostname": "1", "ldap_bind_dn": "CN=svc", "ldap_bind_password": "x",
        "ldap_upn_suffix": "", "ldap_base_dn": base_dn, "ldap_user_attr": "sAMAccountName",
        "ldap_user_filter": "", "ldap_nested_groups": "1" if nested else "0", "ldap_timeout": "5",
        "ldap_admin_rules": "Bibliothek-Admins", "ldap_librarian_rules": "Bibliothek-Verwalter",
        "ldap_login_rules": "Bibliothek-Leser",
    })


def names(found):
    return {ldap_auth.group_cn(dn): src for dn, src in found.items()}


class NestedGroupTests(unittest.TestCase):
    def test_ad_all_mechanisms(self):
        fake = FakeDirectory(ad_entries())
        d = make_dir()
        found = names(d.user_groups(fake, LENA, "lena", fake.user_entry(LENA)))
        self.assertEqual(found, {
            "Lehrer": "direkt",
            "Domänen-Benutzer": "primär",
            "Bibliothek-Verwalter": "verschachtelt",
            "Bibliothek-Leser": "verschachtelt",
        })

    def test_groups_outside_user_ou_are_found(self):
        # Basis-DN ist die Benutzer-OU, Gruppen liegen in OU=Gruppen und CN=Users
        fake = FakeDirectory(ad_entries())
        found = make_dir(base_dn="OU=Benutzer,DC=schule,DC=local").user_groups(fake, LENA, "lena",
                                                                              fake.user_entry(LENA))
        self.assertIn("Bibliothek-Verwalter", names(found))

    def test_without_in_chain_token_groups_still_work(self):
        fake = FakeDirectory(ad_entries(), in_chain=False)
        found = names(make_dir().user_groups(fake, LENA, "lena", fake.user_entry(LENA)))
        self.assertEqual(found.get("Bibliothek-Verwalter"), "verschachtelt")
        self.assertEqual(found.get("Bibliothek-Leser"), "verschachtelt")

    def test_without_token_groups_in_chain_still_works(self):
        fake = FakeDirectory(ad_entries(), token_groups=False)
        found = names(make_dir().user_groups(fake, LENA, "lena", fake.user_entry(LENA)))
        self.assertEqual(found.get("Bibliothek-Verwalter"), "verschachtelt")
        self.assertEqual(found.get("Bibliothek-Leser"), "verschachtelt")  # über die primäre Gruppe

    def test_nested_disabled_only_direct_and_primary(self):
        fake = FakeDirectory(ad_entries())
        found = names(make_dir(nested=False).user_groups(fake, LENA, "lena", fake.user_entry(LENA)))
        self.assertEqual(found, {"Lehrer": "direkt", "Domänen-Benutzer": "primär"})

    def test_circular_groups(self):
        fake = FakeDirectory(ad_entries())
        found = names(make_dir().user_groups(fake, MAX, "max", fake.user_entry(MAX)))
        self.assertEqual(found.get("Ring1"), "direkt")
        self.assertEqual(found.get("Ring2"), "verschachtelt")

    def test_roles_from_nested_groups(self):
        fake = FakeDirectory(ad_entries())
        d = make_dir()
        lena = list(d.user_groups(fake, LENA, "lena", fake.user_entry(LENA)))
        self.assertEqual(ldap_auth.resolve_role(LENA, lena, d.cfg), ("librarian", "Bibliothek-Verwalter"))
        mx = list(d.user_groups(fake, MAX, "max", fake.user_entry(MAX)))
        # Max ist nur über die primäre Gruppe Domänen-Benutzer -> Bibliothek-Leser drin
        self.assertEqual(ldap_auth.resolve_role(MAX, mx, d.cfg)[0], "reader")

    def test_primary_group_as_rule(self):
        fake = FakeDirectory(ad_entries())
        d = make_dir()
        mx = list(d.user_groups(fake, MAX, "max", fake.user_entry(MAX)))
        self.assertEqual(ldap_auth.resolve_role(MAX, mx, {"ldap_admin_rules": "", "ldap_librarian_rules": "", "ldap_login_rules": "Domänen-Benutzer"})[0], "reader")

    def test_openldap_nested(self):
        u = "uid=anna,ou=people,dc=example,dc=org"
        entries = {
            u: {"objectSid": None},
            "cn=team,ou=groups,dc=example,dc=org": {"member": [u]},
            "cn=bibliothek,ou=groups,dc=example,dc=org": {"member": ["cn=team,ou=groups,dc=example,dc=org"]},
            "cn=posix,ou=groups,dc=example,dc=org": {"memberUid": ["anna"]},
        }
        fake = FakeDirectory(entries, in_chain=False, token_groups=False, member_of=False)
        d = make_dir(base_dn="ou=people,dc=example,dc=org")
        entry = {"dn": u, "attributes": {"memberOf": []}, "raw_attributes": {}}
        found = names(d.user_groups(fake, u, "anna", entry))
        self.assertEqual(found, {"team": "direkt", "posix": "direkt", "bibliothek": "verschachtelt"})


class AuthenticateEndToEndTests(unittest.TestCase):
    def test_authenticate_resolves_nested_role(self):
        from unittest import mock
        fake = FakeDirectory(ad_entries(), in_chain=False)  # z. B. ältere Samba-Version
        d = make_dir()
        with mock.patch.object(d, "connect", return_value=fake), \
             mock.patch.object(d, "find_user", return_value=fake.user_entry(LENA)):
            info = d.authenticate("lena", "geheim")
        self.assertEqual(info.role, "librarian")
        self.assertEqual(info.matched_rule, "Bibliothek-Verwalter")
        self.assertEqual(info.group_info[G["dombenutzer"]], "primär")

    def test_login_test_page_shows_origin(self):
        from unittest import mock
        from catalytix import create_app
        from catalytix.models import User, db
        app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        with app.app_context():
            db.drop_all(); db.create_all()
            u = User(username="admin", role="admin"); u.set_password("geheim123")
            db.session.add(u); db.session.commit()
            c = app.test_client()
            c.post("/login", data={"username": "admin", "password": "geheim123"})
            info = ldap_auth.LDAPUser(username="lena", dn=LENA, display_name="Lena", role="librarian",
                                      matched_rule="Bibliothek-Verwalter",
                                      groups=[G["lehrer"], G["verwalter"]],
                                      group_info={G["lehrer"]: "direkt", G["verwalter"]: "verschachtelt"})
            with mock.patch.object(ldap_auth.Directory, "authenticate", return_value=info):
                html = c.post("/admin/verzeichnisdienst", data={
                    "action": "test_user", "test_user": "lena", "test_password": "x",
                    "ldap_server": "ldap://dc1", "ldap_base_dn": BASE}).get_data(as_text=True)
            self.assertIn("verschachtelt", html)
            self.assertIn("<b>Bibliothek-Verwalter</b>", html)
            db.session.remove()


if __name__ == "__main__":
    unittest.main()
