"""Vorschläge beim Tippen: Suchfeld oben (Katalog) und schnelle Personensuche an der Theke."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_people import GDN, Base, ad  # noqa: E402

from catalytix import ldap_auth  # noqa: E402
from catalytix.models import Book, Group, User, db, set_setting  # noqa: E402

PW = "geheim12345"


class CatalogSuggestTests(Base):
    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": PW})

    def titles(self, q):
        return [x["title"] for x in self.c.get(f"/api/katalog?q={q}").get_json()["results"]]

    def test_reader_sees_only_visible_books(self):
        set_setting("catalog_visibility", "own")
        db.session.commit()
        self.login("leser")
        self.assertEqual(self.titles("Fau"), ["Faust"])
        self.assertEqual(self.titles("Nur"), [])                 # Buch der Gruppe IT, Leser ist nicht drin
        leser = User.query.filter_by(username="leser").one()
        leser.groups = [self.group]
        db.session.commit()
        self.assertEqual(self.titles("Nur"), ["Nur IT"])

    def test_result_fields_and_link(self):
        self.login("leser")
        r = self.c.get("/api/katalog?q=Faust").get_json()["results"][0]
        self.assertEqual(r["url"], f"/buecher/{self.book.id}")
        self.assertEqual((r["available"], r["copies"]), (1, 1))

    def test_exact_isbn_and_title_prefix_first(self):
        db.session.add_all([Book(title="Alles über Faust", copies=1),
                            Book(title="Zauberberg", isbn="9783596294336", copies=1)])
        db.session.commit()
        self.assertEqual(self.titles("Faust")[:2], ["Faust", "Alles über Faust"])   # Titelanfang vorne
        self.assertEqual(self.titles("978-3-596-29433-6"), ["Zauberberg"])

    def test_short_query_and_wildcards(self):
        self.assertEqual(self.titles("F"), [])
        self.assertEqual(self.titles("%25%25"), [])                                  # „%%“ wörtlich, nicht alles

    def test_needs_login(self):
        self.c.post("/logout")
        self.assertNotEqual(self.c.get("/api/katalog?q=Faust").status_code, 200)

    def test_more_flag(self):
        db.session.add_all([Book(title=f"Band {i:02d}", copies=1) for i in range(12)])
        db.session.commit()
        r = self.c.get("/api/katalog?q=Band").get_json()
        self.assertEqual(len(r["results"]), 8)
        self.assertTrue(r["more"])

    def test_top_search_field_wired(self):
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("data-catalog-search", html)
        self.assertIn('autocomplete="off"', html)


class FastPeopleSearchTests(Base):
    def test_local_phase_does_not_ask_directory(self):
        cls = self.directory(search=[ad("berta", "Berta Brecht")])
        r = self.c.get("/api/personen?q=ber&verzeichnis=0").get_json()
        cls.return_value.search_users.assert_not_called()
        self.assertTrue(r["directory_pending"])
        r = self.c.get("/api/personen?q=ber").get_json()
        self.assertFalse(r["directory_pending"])
        self.assertIn("berta", [x["username"] for x in r["results"]])

    def test_short_query_not_pending(self):
        self.directory()
        r = self.c.get("/api/personen?q=be&verzeichnis=0").get_json()
        self.assertFalse(r["directory_pending"])


class SearchGroupCacheTests(Base):
    def setUp(self):
        super().setUp()
        ldap_auth._search_groups.clear()
        self.d = ldap_auth.Directory({"ldap_server": "ldap://dc", "ldap_base_dn": "DC=x,DC=de",
                                      "ldap_bind_dn": "CN=svc,DC=x,DC=de", "ldap_user_attr": "sAMAccountName"})
        self.entry = {"type": "searchResEntry", "dn": "CN=Berta,DC=x,DC=de",
                      "attributes": {"sAMAccountName": "berta", "displayName": "Berta", "memberOf": [GDN],
                                     "userAccountControl": 512, "primaryGroupID": "513"}}

    def run_search(self):
        conn = mock.Mock()
        conn.response = [self.entry]
        with mock.patch.object(self.d, "service_connection", return_value=conn), \
                mock.patch.object(self.d, "user_groups", return_value={GDN: "direkt"}) as ug, \
                mock.patch.object(ldap_auth, "resolve_role", return_value=("reader", "x")):
            self.d.search_users("ber")
            self.d.search_users("bert")
        return ug.call_count

    def test_groups_resolved_once_while_typing(self):
        self.assertEqual(self.run_search(), 1)

    def test_changed_membership_resolved_again(self):
        self.run_search()
        self.entry["attributes"]["memberOf"] = [GDN, "CN=Neu,DC=x,DC=de"]
        self.assertEqual(self.run_search(), 1)              # neuer Schlüssel → einmal neu aufgelöst

    def test_login_check_never_cached(self):
        conn = mock.Mock()
        with mock.patch.object(self.d, "service_connection", return_value=conn), \
                mock.patch.object(self.d, "find_user", return_value=self.entry), \
                mock.patch.object(self.d, "user_groups", return_value={GDN: "direkt"}) as ug, \
                mock.patch.object(ldap_auth, "resolve_role", return_value=("reader", "x")):
            self.d.check_account("berta")
            self.d.check_account("berta")
        self.assertEqual(ug.call_count, 2)


if __name__ == "__main__":
    unittest.main()
