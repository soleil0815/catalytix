"""3.0.4: bare AD group names in the administrator and librarian rules are turned into the DN of exactly one group
when saving – a bare name matches every group of that name in the directory."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import ldap_auth  # noqa: E402
from catalytix.models import get_setting, set_setting, db  # noqa: E402

ADMINS = "CN=Bibliothek-Admins,OU=Gruppen,DC=firma,DC=local"


class SaveTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.login("admin")

    def save(self, admin_rules, bind_dn="CN=svc,DC=firma,DC=local", **extra):
        data = {"action": "save", "ldap_enabled": "1", "ldap_server": "ldap://dc1.firma.local",
                "ldap_base_dn": "DC=firma,DC=local", "ldap_bind_dn": bind_dn,
                "ldap_admin_rules": admin_rules, "ldap_librarian_rules": "", "ldap_login_rules": "Domänen-Benutzer"}
        data.update(extra)
        return self.c.post("/admin/verzeichnisdienst", data=data, follow_redirects=True).get_data(as_text=True)

    def test_unique_name_becomes_dn(self):
        with mock.patch.object(ldap_auth.Directory, "find_groups_by_name", return_value=[ADMINS]) as f:
            self.save("Bibliothek-Admins")
        f.assert_called_once_with("Bibliothek-Admins")
        self.assertEqual(get_setting("ldap_admin_rules"), ADMINS)
        self.assertEqual(get_setting("ldap_login_rules"), "Domänen-Benutzer")    # reader rules stay as they are

    def test_ambiguous_name_is_not_saved(self):
        other = "CN=Bibliothek-Admins,OU=Klasse7a,DC=firma,DC=local"
        with mock.patch.object(ldap_auth.Directory, "find_groups_by_name", return_value=[ADMINS, other]):
            html = self.save("Bibliothek-Admins")
        self.assertIn("mehrfach", html)
        self.assertIn("OU=Klasse7a", html)
        self.assertEqual(get_setting("ldap_admin_rules"), "")
        self.assertIn("Bibliothek-Admins</textarea>", html)                 # entered values are kept in the form

    def test_unknown_name_is_not_saved(self):
        with mock.patch.object(ldap_auth.Directory, "find_groups_by_name", return_value=[]):
            html = self.save("Tippfehler")
        self.assertIn("nicht gefunden", html)
        self.assertEqual(get_setting("ldap_admin_rules"), "")

    def test_dn_and_ou_rules_need_no_lookup(self):
        with mock.patch.object(ldap_auth.Directory, "find_groups_by_name", side_effect=AssertionError) as f:
            self.save(ADMINS + "\nOU=IT,DC=firma,DC=local")
        f.assert_not_called()
        self.assertIn(ADMINS, get_setting("ldap_admin_rules"))

    def test_without_service_account_saved_with_warning(self):
        html = self.save("Bibliothek-Admins", bind_dn="")
        self.assertEqual(get_setting("ldap_admin_rules"), "Bibliothek-Admins")
        self.assertIn("kein Dienstkonto", html)

    def test_directory_error_saves_with_warning(self):
        with mock.patch.object(ldap_auth.Directory, "find_groups_by_name",
                               side_effect=ldap_auth.LDAPError("DC nicht erreichbar")):
            html = self.save("Bibliothek-Admins")
        self.assertEqual(get_setting("ldap_admin_rules"), "Bibliothek-Admins")
        self.assertIn("DC nicht erreichbar", html)

    def test_stored_bare_name_shows_a_warning(self):
        set_setting("ldap_librarian_rules", "Bibliothek-Verwalter")
        db.session.commit()
        html = self.c.get("/admin/verzeichnisdienst").get_data(as_text=True)
        self.assertIn("bloßer Gruppenname (Bibliothek-Verwalter)", html)


class FindGroupsTests(BaseCase):
    def test_exact_name_only_and_escaped(self):
        conn = mock.Mock()
        conn.extend.standard.paged_search.return_value = iter([
            {"type": "searchResEntry", "dn": ADMINS},
            {"type": "searchResEntry", "dn": "CN=Bibliothek-Admins-Alt,OU=Gruppen,DC=firma,DC=local"},
            {"type": "searchResRef", "dn": ""}])
        d = ldap_auth.Directory({"ldap_server": "ldap://dc1", "ldap_base_dn": "OU=Leute,DC=firma,DC=local",
                                 "ldap_bind_dn": "CN=svc,DC=firma,DC=local"})
        with mock.patch.object(ldap_auth.Directory, "service_connection", return_value=conn):
            self.assertEqual(d.find_groups_by_name("bibliothek-admins*"), [])
            conn.extend.standard.paged_search.return_value = iter([{"type": "searchResEntry", "dn": ADMINS}])
            self.assertEqual(d.find_groups_by_name("Bibliothek-Admins"), [ADMINS])
        base, flt = conn.extend.standard.paged_search.call_args_list[0].args[:2]
        self.assertEqual(base, "DC=firma,DC=local")                       # whole domain, not just the user OU
        self.assertIn("(cn=bibliothek-admins\\2a)", flt)                    # "*" escaped – no wildcard search
        conn.unbind.assert_called()


if __name__ == "__main__":
    unittest.main()
