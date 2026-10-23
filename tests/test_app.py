"""Tests für CATALYTIX – ausführen mit:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="catalytix-test-")
os.environ["CATALYTIX_DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = "sqlite://"

from catalytix import create_app, isbn as isbnlib  # noqa: E402
from catalytix import ldap_auth  # noqa: E402
from catalytix.models import Book, Group, Loan, User, db, set_setting  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                               "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.app.config.pop("_setup_done", None)
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # Hilfen
    def make_user(self, name, role="reader", pw="geheim123", groups=()):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password(pw)
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name, pw="geheim123"):
        self.c.post("/logout")
        return self.c.post("/login", data={"username": name, "password": pw}, follow_redirects=True)


class SetupAndAuthTests(BaseCase):
    def test_setup_flow(self):
        r = self.c.get("/", follow_redirects=False)
        self.assertIn("/setup", r.headers["Location"])
        from catalytix import setupcode
        r = self.c.post("/setup", data={"username": "admin", "password": "supersecret", "password2": "supersecret",
                                       "library_name": "Schulbibliothek", "setup_code": setupcode.ensure()},
                        follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Schulbibliothek", r.get_data(as_text=True))
        self.assertTrue(User.query.filter_by(username="admin", role="admin").first())
        # Setup danach nicht mehr erreichbar, der Code ist weg
        self.assertIn("/login", self.c.get("/setup").headers["Location"])
        self.assertEqual(setupcode.current(), "")

    def test_setup_needs_code(self):
        from catalytix import setupcode
        code = setupcode.ensure()
        data = {"username": "evil", "password": "supersecret", "password2": "supersecret"}
        r = self.c.post("/setup", data=dict(data, setup_code="AAAA-BBBB-CCCC"))
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/setup", data=data)
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(User.query.filter_by(username="evil").first())
        # Kleinbuchstaben und ohne Bindestriche gehen auch
        r = self.c.post("/setup", data=dict(data, username="admin", setup_code=code.lower().replace("-", "")))
        self.assertEqual(r.status_code, 302)

    def test_setup_code_guessing_throttled(self):
        from catalytix import setupcode
        setupcode.ensure()
        data = {"username": "evil", "password": "supersecret", "password2": "supersecret", "setup_code": "X"}
        codes = [self.c.post("/setup", data=data).status_code for _ in range(12)]
        self.assertIn(429, codes)

    def test_setup_code_cli(self):
        from catalytix import setupcode
        r = self.app.test_cli_runner().invoke(args=["setup-code"])
        self.assertEqual(r.output.strip(), setupcode.current())
        self.assertRegex(r.output.strip(), r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")

    def test_login_logout_and_wrong_password(self):
        self.make_user("admin", "admin")
        r = self.c.post("/login", data={"username": "admin", "password": "falsch"})
        self.assertEqual(r.status_code, 401)
        r = self.login("ADMIN")  # Groß-/Kleinschreibung egal
        self.assertIn("Hallo", r.get_data(as_text=True))
        self.c.post("/logout")
        self.assertIn("/login", self.c.get("/").headers["Location"])

    def test_inactive_user_cannot_login(self):
        self.make_user("admin", "admin")
        u = self.make_user("max")
        u.active = False
        db.session.commit()
        r = self.c.post("/login", data={"username": "max", "password": "geheim123"})
        self.assertEqual(r.status_code, 401)

    def test_open_redirect_blocked(self):
        self.make_user("admin", "admin")
        r = self.c.post("/login?next=https://evil.example", data={"username": "admin", "password": "geheim123"})
        self.assertEqual(r.headers["Location"], "/")


class PermissionTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.g7 = Group(name="Klasse 7a")
        self.g8 = Group(name="Klasse 8b")
        db.session.add_all([self.g7, self.g8])
        db.session.commit()
        self.admin = self.make_user("admin", "admin")
        self.lib7 = self.make_user("lehrer7", "librarian", groups=[self.g7])
        self.libglobal = self.make_user("verwalter", "librarian")
        self.r7 = self.make_user("schueler7", groups=[self.g7])
        self.r8 = self.make_user("schueler8", groups=[self.g8])
        self.b7 = Book(title="Buch 7a", group_id=self.g7.id, copies=1, isbn="9783161484100")
        self.b8 = Book(title="Buch 8b", group_id=self.g8.id, copies=1)
        self.ball = Book(title="Allgemeines Buch", copies=2)
        db.session.add_all([self.b7, self.b8, self.ball])
        db.session.commit()

    def test_reader_catalog_only_own_groups(self):
        self.login("schueler7")
        html = self.c.get("/buecher").get_data(as_text=True)
        self.assertIn("Buch 7a", html)
        self.assertIn("Allgemeines Buch", html)
        self.assertNotIn("Buch 8b", html)
        self.assertEqual(self.c.get(f"/buecher/{self.b8.id}").status_code, 404)

    def test_catalog_visibility_all(self):
        set_setting("catalog_visibility", "all")
        db.session.commit()
        self.login("schueler7")
        self.assertIn("Buch 8b", self.c.get("/buecher").get_data(as_text=True))
        # Ausleihen trotzdem verboten
        self.c.post(f"/buecher/{self.b8.id}/ausleihen")
        self.assertEqual(Loan.query.count(), 0)

    def test_reader_borrow_rules(self):
        self.login("schueler7")
        self.c.post(f"/buecher/{self.b7.id}/ausleihen")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 1)
        self.c.post(f"/buecher/{self.b8.id}/ausleihen")  # fremde Gruppe
        self.assertEqual(Loan.query.filter_by(book_id=self.b8.id).count(), 0)
        self.c.post(f"/buecher/{self.ball.id}/ausleihen")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 2)
        # zweimal dasselbe Buch geht nicht
        self.c.post(f"/buecher/{self.ball.id}/ausleihen")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 2)

    def test_no_copy_available(self):
        self.login("schueler7")
        self.c.post(f"/buecher/{self.b7.id}/ausleihen")
        self.login("lehrer7")
        r = self.c.post(f"/buecher/{self.b7.id}/ausleihen", data={"user_id": self.lib7.id}, follow_redirects=True)
        self.assertIn("Kein Exemplar verfügbar", r.get_data(as_text=True))

    def test_reader_cannot_manage(self):
        self.login("schueler7")
        self.assertEqual(self.c.get("/buecher/neu").status_code, 403)
        self.assertEqual(self.c.get("/admin/benutzer").status_code, 403)
        self.assertEqual(self.c.post(f"/buecher/{self.b7.id}/loeschen").status_code, 403)
        # Für andere ausleihen ist verboten
        self.assertEqual(self.c.post(f"/buecher/{self.b7.id}/ausleihen", data={"user_id": self.r8.id}).status_code, 403)

    def test_group_librarian_scope(self):
        self.login("lehrer7")
        self.assertEqual(self.c.get(f"/buecher/{self.b7.id}/bearbeiten").status_code, 200)
        self.assertEqual(self.c.get(f"/buecher/{self.b8.id}/bearbeiten").status_code, 403)
        self.assertEqual(self.c.get("/admin/benutzer").status_code, 403)
        # darf keine Bücher in fremde Gruppen verschieben
        r = self.c.post(f"/buecher/{self.b7.id}/bearbeiten", data={"title": "X", "group_id": self.g8.id, "copies": 1})
        self.assertEqual(r.status_code, 403)
        # Verleihen an Mitglied der eigenen Gruppe
        self.c.post(f"/buecher/{self.b7.id}/ausleihen", data={"user_id": self.r7.id})
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 1)

    def test_librarian_cannot_lend_to_wrong_group(self):
        self.login("verwalter")
        r = self.c.post(f"/buecher/{self.b7.id}/ausleihen", data={"user_id": self.r8.id}, follow_redirects=True)
        self.assertIn("darf dieses Buch nicht ausleihen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)

    def test_global_librarian_manages_all(self):
        self.login("verwalter")
        self.assertEqual(self.c.get(f"/buecher/{self.b8.id}/bearbeiten").status_code, 200)

    def test_return_extend(self):
        set_setting("self_return", "0")
        set_setting("max_extensions", "1")
        db.session.commit()
        self.login("schueler7")
        self.c.post(f"/buecher/{self.b7.id}/ausleihen")
        loan = Loan.query.one()
        due = loan.due_at
        self.c.post(f"/ausleihen/{loan.id}/verlaengern")
        db.session.refresh(loan)
        self.assertGreater(loan.due_at, due)
        r = self.c.post(f"/ausleihen/{loan.id}/verlaengern", follow_redirects=True)
        self.assertIn("maximale Anzahl", r.get_data(as_text=True))
        self.assertEqual(self.c.post(f"/ausleihen/{loan.id}/rueckgabe").status_code, 403)
        self.login("lehrer7")
        self.c.post(f"/ausleihen/{loan.id}/rueckgabe")
        db.session.refresh(loan)
        self.assertIsNotNone(loan.returned_at)

    def test_max_loans(self):
        set_setting("max_loans", "1")
        db.session.commit()
        self.login("schueler7")
        self.c.post(f"/buecher/{self.b7.id}/ausleihen")
        r = self.c.post(f"/buecher/{self.ball.id}/ausleihen", follow_redirects=True)
        self.assertIn("maximale Anzahl", r.get_data(as_text=True))

    def test_desk_lend_and_return_by_isbn(self):
        self.login("lehrer7")
        r = self.c.post("/theke", data={"modus": "ausleihe", "benutzer": "schueler7", "code": "978-3-16-148410-0",
                                        "auto": "1"}, follow_redirects=True)
        self.assertIn("verliehen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id, returned_at=None).count(), 1)
        r = self.c.post("/theke", data={"modus": "rueckgabe", "code": "3161484100", "auto": "1"}, follow_redirects=True)
        self.assertIn("zurückgenommen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.filter_by(returned_at=None).count(), 0)

    def test_overdue_listing(self):
        loan = Loan(book=self.b7, user=self.r7, due_at=date.today() - timedelta(days=3))
        db.session.add(loan)
        db.session.commit()
        self.login("admin")
        html = self.c.get("/ausleihen?ansicht=ueberfaellig").get_data(as_text=True)
        self.assertIn("3 Tg. überfällig", html)
        self.assertIn("Buch 7a", self.c.get("/").get_data(as_text=True))

    def test_all_pages_render(self):
        self.login("admin")
        loan = Loan(book=self.b7, user=self.r7, due_at=date.today())
        db.session.add(loan)
        db.session.commit()
        for url in ["/", "/buecher", "/buecher?status=verfuegbar", "/buecher?q=buch&sort=new", f"/buecher/{self.b7.id}",
                    "/buecher/neu", "/buecher/neu?isbn=9783161484100", f"/buecher/{self.b7.id}/bearbeiten",
                    "/buecher/schnellerfassung", "/ausleihen", "/ausleihen?ansicht=verlauf", "/ausleihen?nur=meine",
                    "/theke", "/theke?modus=rueckgabe", "/theke?benutzer=schueler7", "/profil",
                    "/admin/benutzer", "/admin/benutzer/neu", f"/admin/benutzer/{self.r7.id}", "/admin/gruppen",
                    "/admin/gruppen/neu", f"/admin/gruppen/{self.g7.id}", "/admin/einstellungen",
                    "/admin/verzeichnisdienst", "/buecher/export.csv"]:
            with self.subTest(url=url):
                self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get("/buecher/9999").status_code, 404)


class BookTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.login("admin")
        set_setting("cache_covers", "0")
        db.session.commit()

    def test_create_edit_delete(self):
        r = self.c.post("/buecher/neu", data={"title": "Der Process", "authors": "Franz Kafka",
                                               "isbn": "3-16-148410-X", "copies": "2", "pages": "300"})
        book = Book.query.one()
        self.assertEqual(book.copies, 2)
        self.assertEqual(book.pages, 300)
        self.c.post(f"/buecher/{book.id}/bearbeiten", data={"title": "Der Proceß", "copies": "3"})
        db.session.refresh(book)
        self.assertEqual(book.title, "Der Proceß")
        self.c.post(f"/buecher/{book.id}/loeschen")
        self.assertEqual(Book.query.count(), 0)

    def test_title_required(self):
        r = self.c.post("/buecher/neu", data={"title": ""}, follow_redirects=True)
        self.assertIn("Titel ist erforderlich", r.get_data(as_text=True))

    def test_cannot_delete_lent_book(self):
        u = self.make_user("leser")
        b = Book(title="T", copies=1)
        db.session.add(b)
        db.session.add(Loan(book=b, user=u, due_at=date.today()))
        db.session.commit()
        self.c.post(f"/buecher/{b.id}/loeschen")
        self.assertEqual(Book.query.count(), 1)

    def test_isbn_api_and_quick_add(self):
        fake = {"isbn": "9783161484100", "title": "Testbuch", "authors": "A. Autor", "found": True,
                "sources": ["Open Library"], "errors": []}
        with mock.patch.object(isbnlib, "lookup", return_value=fake):
            r = self.c.get("/api/isbn/978-3-16-148410-0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["data"]["title"], "Testbuch")
        self.assertEqual(r.json["existing"], [])
        self.assertEqual(self.c.get("/api/isbn/9783161484101").status_code, 400)

        r = self.c.post("/api/buecher", json={**fake, "location": "Regal 1"})
        self.assertTrue(r.json["ok"])
        b = Book.query.one()
        self.assertEqual(b.location, "Regal 1")
        r = self.c.get("/api/isbn/9783161484100?lookup=0")
        self.assertEqual(len(r.json["existing"]), 1)
        r = self.c.post(f"/api/buecher/{b.id}/exemplar")
        self.assertEqual(r.json["copies"], 2)


class IsbnTests(unittest.TestCase):
    def test_validation(self):
        self.assertTrue(isbnlib.is_valid_isbn("978-3-16-148410-0"))
        self.assertTrue(isbnlib.is_valid_isbn("3-16-148410-X"))
        self.assertFalse(isbnlib.is_valid_isbn("978-3-16-148410-1"))
        self.assertEqual(isbnlib.to_isbn13("316148410X"), "9783161484100")
        self.assertEqual(isbnlib.to_isbn10("9783161484100"), "316148410X")

    def test_dnb_parser(self):
        xml = b"""<?xml version="1.0"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><records><record><recordData>
<record xmlns="http://www.loc.gov/MARC21/slim">
 <datafield tag="041"><subfield code="a">ger</subfield></datafield>
 <datafield tag="100"><subfield code="a">Kehlmann, Daniel</subfield><subfield code="4">aut</subfield></datafield>
 <datafield tag="245"><subfield code="a">Die Vermessung der Welt</subfield><subfield code="b">Roman /</subfield></datafield>
 <datafield tag="264"><subfield code="b">Rowohlt</subfield><subfield code="c">[2005]</subfield></datafield>
 <datafield tag="300"><subfield code="a">302 S.</subfield></datafield>
</record></recordData></record></records></searchRetrieveResponse>"""
        resp = mock.Mock(content=xml, status_code=200)
        resp.raise_for_status = lambda: None
        with mock.patch.object(isbnlib.requests, "get", return_value=resp):
            d = isbnlib.lookup_dnb("9783498035280")
        self.assertEqual(d["title"], "Die Vermessung der Welt")
        self.assertEqual(d["subtitle"], "Roman")
        self.assertEqual(d["authors"], "Daniel Kehlmann")
        self.assertEqual(d["publisher"], "Rowohlt")
        self.assertEqual(d["published"], "2005")
        self.assertEqual(d["pages"], 302)
        self.assertEqual(d["language"], "ger")

    def test_merge_sources(self):
        with mock.patch.dict(isbnlib.SOURCES, {
            "a": ("A", lambda i: {"title": "T", "authors": "X"}),
            "b": ("B", lambda i: (_ for _ in ()).throw(RuntimeError("offline"))),
            "c": ("C", lambda i: {"title": "anders", "description": "Beschreibung"}),
        }, clear=True):
            d = isbnlib.lookup("9783161484100", ["a", "b", "c"])
        self.assertEqual(d["title"], "T")
        self.assertEqual(d["description"], "Beschreibung")
        self.assertEqual(d["sources"], ["A", "C"])
        self.assertEqual(len(d["errors"]), 1)


class LdapTests(BaseCase):
    def test_rule_matching(self):
        dn = "CN=Max Muster,OU=7a,OU=Schueler,DC=schule,DC=local"
        groups = ["CN=Bib-Admins,OU=Gruppen,DC=schule,DC=local"]
        cfg = {"ldap_admin_rules": "cn=bib-admins, ou=Gruppen, dc=schule, dc=local",
               "ldap_librarian_rules": "OU=Lehrer,DC=schule,DC=local",
               "ldap_login_rules": "OU=Schueler,DC=schule,DC=local"}
        self.assertEqual(ldap_auth.resolve_role(dn, groups, cfg)[0], "admin")
        self.assertEqual(ldap_auth.resolve_role(dn, [], cfg)[0], "reader")
        self.assertIsNone(ldap_auth.resolve_role("CN=X,OU=Gaeste,DC=schule,DC=local", [], cfg)[0])
        # Teilstring einer OU darf nicht greifen
        self.assertFalse(ldap_auth.rule_matches("OU=Schueler,DC=schule,DC=local",
                                                "CN=Y,OU=AltSchueler,DC=schule,DC=local", set()))

    def test_group_name_rules(self):
        dn = "CN=Max,OU=Benutzer,DC=schule,DC=local"
        groups = ["CN=Bibliothek-Verwalter,OU=Gruppen,DC=schule,DC=local",
                  "CN=Domänen-Benutzer,CN=Users,DC=schule,DC=local"]
        cfg = {"ldap_admin_rules": "Bibliothek-Admins", "ldap_librarian_rules": "bibliothek-verwalter",
               "ldap_login_rules": "Domänen-Benutzer"}
        self.assertEqual(ldap_auth.resolve_role(dn, groups, cfg), ("librarian", "bibliothek-verwalter"))
        self.assertEqual(ldap_auth.resolve_role(dn, groups[1:], cfg)[0], "reader")
        self.assertIsNone(ldap_auth.resolve_role(dn, [], cfg)[0])
        # Teilname greift nicht
        self.assertFalse(ldap_auth.rule_matches("Bibliothek", dn, {ldap_auth.normalize_dn(groups[0])}))

    def test_group_sync_by_name(self):
        self.make_user("admin", "admin")
        g = Group(name="Klasse 7a", ldap_dn="Klasse7a")
        db.session.add(g)
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="lena", dn="CN=Lena,DC=x", groups=["CN=Klasse7a,OU=G,DC=x"], role="reader")
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            self.c.post("/login", data={"username": "lena", "password": "x"})
        self.assertEqual([x.name for x in User.query.filter_by(username="lena").one().groups], ["Klasse 7a"])

    def test_admin_lists_ad_groups(self):
        self.make_user("admin", "admin")
        self.login("admin")
        fake = [{"name": "Bibliothek-Admins", "dn": "CN=Bibliothek-Admins,DC=x", "description": "Admins"}]
        with mock.patch.object(ldap_auth.Directory, "list_groups", return_value=fake) as lg:
            html = self.c.post("/admin/verzeichnisdienst", data={
                "action": "list_groups", "group_query": "Bib", "ldap_server": "ldap://dc1",
                "ldap_base_dn": "DC=x"}).get_data(as_text=True)
            lg.assert_called_once_with("Bib")
        self.assertIn('data-name="Bibliothek-Admins"', html)

    def test_ldap_login_creates_user_and_syncs_groups(self):
        self.make_user("admin", "admin")
        g = Group(name="Klasse 7a", ldap_dn="CN=Klasse7a,OU=Gruppen,DC=schule,DC=local")
        manual = Group(name="Leseclub")
        db.session.add_all([g, manual])
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="mmuster", dn="CN=Max,OU=Schueler,DC=schule,DC=local",
                                  display_name="Max Muster", email="max@schule.local",
                                  groups=["cn=klasse7a,ou=gruppen,dc=schule,dc=local"], role="reader")
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "mmuster", "password": "x"}, follow_redirects=True)
        self.assertIn("Hallo, Max", r.get_data(as_text=True))
        u = User.query.filter_by(username="mmuster").one()
        self.assertEqual(u.auth_source, "ldap")
        self.assertEqual([x.name for x in u.groups], ["Klasse 7a"])
        # manuell zugeordnete Gruppe bleibt, AD-Gruppe wird bei Austritt entfernt
        u.groups.append(manual)
        db.session.commit()
        info.groups, info.role = [], "librarian"
        self.c.post("/logout")
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            self.c.post("/login", data={"username": "mmuster", "password": "x"})
        db.session.refresh(u)
        self.assertEqual([x.name for x in u.groups], ["Leseclub"])
        self.assertEqual(u.role, "librarian")

    def test_ldap_login_denied_without_rule(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="gast", dn="CN=Gast,DC=x", role=None)
        with mock.patch("catalytix.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "gast", "password": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Benutzername oder Passwort ist falsch", r.get_data(as_text=True))
        self.assertIsNone(User.query.filter_by(username="gast").first())

    def test_ldap_error_does_not_leak(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        with mock.patch("catalytix.views.main.authenticate", side_effect=ldap_auth.LDAPError("geheimer DC")):
            r = self.c.post("/login", data={"username": "a", "password": "b"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("geheimer DC", r.get_data(as_text=True))

    def test_local_admin_wins_over_ldap(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        with mock.patch("catalytix.views.main.authenticate") as auth:
            r = self.login("admin")
            auth.assert_not_called()
        self.assertIn("Hallo", r.get_data(as_text=True))

    def test_empty_password_rejected(self):
        d = ldap_auth.Directory({"ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x"})
        with self.assertRaises(ldap_auth.LDAPError):
            d.connect("CN=x", "")

    def test_unreachable_server_gives_ldap_error(self):
        d = ldap_auth.Directory({"ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x",
                                 "ldap_bind_dn": "CN=svc", "ldap_bind_password": "pw", "ldap_timeout": "2"})
        with self.assertRaises(ldap_auth.LDAPError):
            d.test_connection()

    def test_admin_ldap_page_test_and_save(self):
        self.make_user("admin", "admin")
        self.login("admin")
        r = self.c.post("/admin/verzeichnisdienst", data={
            "action": "test_bind", "ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x",
            "ldap_bind_dn": "CN=svc", "ldap_bind_password": "pw", "ldap_timeout": "2"})
        self.assertIn("Test fehlgeschlagen", r.get_data(as_text=True))
        self.c.post("/admin/verzeichnisdienst", data={
            "action": "save", "ldap_enabled": "1", "ldap_server": "ldap://dc1", "ldap_base_dn": "DC=x",
            "ldap_bind_password": "pw1", "ldap_login_rules": "OU=A,DC=x\n\n  OU=B,DC=x  "})
        from catalytix.models import get_setting
        self.assertEqual(get_setting("ldap_login_rules"), "OU=A,DC=x\nOU=B,DC=x")
        self.assertEqual(get_setting("ldap_bind_password"), "pw1")
        # leeres Passwortfeld behält das gespeicherte
        self.c.post("/admin/verzeichnisdienst", data={"action": "save", "ldap_server": "ldap://dc1", "ldap_base_dn": "DC=x"})
        self.assertEqual(get_setting("ldap_bind_password"), "pw1")
        self.assertNotIn("pw1", self.c.get("/admin/verzeichnisdienst").get_data(as_text=True))


class AdminTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")
        self.login("admin")

    def test_user_crud(self):
        g = Group(name="G1")
        db.session.add(g)
        db.session.commit()
        self.c.post("/admin/benutzer/neu", data={"username": "neu", "password": "1234567890", "role": "librarian",
                                                 "active": "1", "groups": [str(g.id)]})
        u = User.query.filter_by(username="neu").one()
        self.assertEqual(u.role, "librarian")
        self.assertEqual(u.groups, [g])
        r = self.c.post("/admin/benutzer/neu", data={"username": "NEU", "password": "1234567890", "role": "reader"},
                        follow_redirects=True)
        self.assertIn("bereits vergeben", r.get_data(as_text=True))
        self.c.post(f"/admin/benutzer/{u.id}/loeschen")
        self.assertIsNone(User.query.filter_by(username="neu").first())

    def test_cannot_demote_self_or_last_admin(self):
        r = self.c.post(f"/admin/benutzer/{self.admin.id}", data={"username": "admin", "role": "reader", "active": "1"},
                        follow_redirects=True)
        self.assertIn("nicht entziehen", r.get_data(as_text=True))
        db.session.refresh(self.admin)
        self.assertEqual(self.admin.role, "admin")

    def test_group_delete_makes_books_general(self):
        g = Group(name="G")
        db.session.add(g)
        db.session.commit()
        b = Book(title="B", group_id=g.id)
        db.session.add(b)
        db.session.commit()
        self.c.post(f"/admin/gruppen/{g.id}/loeschen")
        db.session.refresh(b)
        self.assertIsNone(b.group_id)

    def test_settings(self):
        self.c.post("/admin/einstellungen", data={"library_name": "Stadtbücherei", "loan_days": "21",
                                                  "isbn_sources": ["google", "dnb"], "isbn_order": "dnb,openlibrary,google"})
        from catalytix.models import get_setting
        self.assertEqual(get_setting("loan_days"), "21")
        self.assertEqual(get_setting("isbn_sources"), "dnb,google")
        self.assertEqual(get_setting("self_checkout"), "0")


if __name__ == "__main__":
    unittest.main()
