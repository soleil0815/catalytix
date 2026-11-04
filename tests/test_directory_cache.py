"""AD people cache: background sync, search from the database, updating accounts."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_people import GDN, Base  # noqa: E402

from catalytix import people  # noqa: E402
from catalytix.ldap_auth import LDAPError, LDAPUser  # noqa: E402
from catalytix.models import Book, DirectoryPerson, Loan, User, db, get_setting, set_setting  # noqa: E402

PW = "geheim12345"


def person(name, display, guid="", groups=(GDN,), role="reader", email=None):
    return LDAPUser(username=name, dn=f"CN={display},OU=Leute,DC=x,DC=de", display_name=display,
                    email=email if email is not None else f"{name}@x.de", groups=list(groups), role=role, guid=guid)


class CacheBase(Base):
    def scan(self, people_list, error=None):
        cls = self.directory()
        if error:
            cls.return_value.scan_people.side_effect = error
        else:
            cls.return_value.scan_people.return_value = [(p, "k-" + p.username, True) for p in people_list]
        return cls

    def sync(self, people_list, error=None, manual=False):
        cls = self.scan(people_list, error)
        return people.sync_directory(manual=manual), cls

    def ldap_user(self, name, display="", guid="", active=True):
        u = User(username=name, display_name=display or name, role="reader", auth_source="ldap", ldap_guid=guid,
                 active=active)
        db.session.add(u)
        db.session.commit()
        return u


class SyncTests(CacheBase):
    def test_sync_fills_cache_and_search_needs_no_ad(self):
        stats, cls = self.sync([person("berta", "Berta Brecht"), person("carl", "Carl Ohne")])
        self.assertEqual((stats["count"], stats["added"]), (2, 2))
        cls.return_value.search_users.reset_mock()
        r = self.c.get("/api/personen?q=ber&verzeichnis=0").get_json()
        self.assertEqual([(x["username"], x["new"]) for x in r["results"] if x["username"] == "berta"],
                         [("berta", True)])
        self.assertFalse(r["directory_pending"])                    # cache knows someone → no live AD
        cls.return_value.search_users.assert_not_called()
        self.assertEqual(next(x for x in r["results"] if x["username"] == "berta")["groups"], ["IT"])

    def test_two_characters_enough_with_cache(self):
        self.sync([person("berta", "Berta Brecht")])
        r = self.c.get("/api/personen?q=be&verzeichnis=0").get_json()
        self.assertIn("berta", [x["username"] for x in r["results"]])

    def test_unknown_to_cache_falls_back_to_live_ad(self):
        self.sync([person("berta", "Berta Brecht")])
        r = self.c.get("/api/personen?q=zzz-neu&verzeichnis=0").get_json()
        self.assertTrue(r["directory_pending"])

    def test_disabled_and_deleted_removed(self):
        self.sync([person("berta", "Berta Brecht"), person("carl", "Carl Ohne")])
        stats, _cls = self.sync([person("berta", "Berta Brecht")])     # carl disabled/deleted in AD
        self.assertEqual(stats["removed"], 1)
        self.assertEqual([p.username for p in DirectoryPerson.query], ["berta"])

    def test_name_and_group_changes_updated(self):
        self.sync([person("berta", "Berta Brecht")])
        stats, _cls = self.sync([person("berta", "Berta Weill", groups=(), email="neu@x.de")])
        self.assertEqual(stats["changed"], 1)
        row = DirectoryPerson.query.one()
        self.assertEqual((row.display_name, row.email, row.groups), ("Berta Weill", "neu@x.de", ""))

    def test_error_keeps_cache(self):
        self.sync([person("berta", "Berta Brecht")])
        stats, _cls = self.sync([], error=LDAPError("Abgleich unvollständig"))
        self.assertIn("unvollständig", stats["error"])
        self.assertEqual(DirectoryPerson.query.count(), 1)
        self.assertIn("unvollständig", people.result_text(people.last_result()))

    def test_runs_not_twice_at_once(self):
        token = people._acquire_run()
        self.assertTrue(token)
        stats, cls = self.sync([person("berta", "Berta Brecht")])
        self.assertTrue(stats.get("busy"))
        cls.return_value.scan_people.assert_not_called()
        people._release_run(token)

    def test_stale_lock_ignored(self):
        set_setting("dircache_running", (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"))
        db.session.commit()
        stats, _cls = self.sync([person("berta", "Berta Brecht")])
        self.assertEqual(stats["count"], 1)

    def test_switching_off_clears_cache(self):
        self.sync([person("berta", "Berta Brecht")])
        set_setting("ldap_cache_enabled", "0")
        db.session.commit()
        people.run_scheduled_sync()
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertFalse(people.cache_ready())

    def test_find_cached_uses_cache(self):
        cls = self.directory(account=None)
        self.sync([person("berta", "Berta Brecht")])
        cls.return_value.check_account.reset_mock()
        cand = people.find("berta", cached=True)
        self.assertTrue(getattr(cand, "is_candidate", False))
        cls.return_value.check_account.assert_not_called()


class ScheduleTests(CacheBase):
    def test_first_run_immediately_then_once_per_window(self):
        self.scan([person("berta", "Berta Brecht")])
        now = datetime(2026, 10, 23, 10, 5)
        self.assertIsNotNone(people.run_scheduled_sync(now))
        self.assertIsNone(people.run_scheduled_sync(now + timedelta(minutes=10)))   # same time window
        self.assertIsNotNone(people.run_scheduled_sync(now + timedelta(hours=1)))

    def test_interval_hours(self):
        set_setting("ldap_cache_hours", "4")
        db.session.commit()
        self.scan([person("berta", "Berta Brecht")])
        now = datetime(2026, 10, 23, 9, 0)
        people.run_scheduled_sync(now)
        self.assertIsNone(people.run_scheduled_sync(now + timedelta(hours=2)))     # 8–12 o'clock: one run
        self.assertIsNotNone(people.run_scheduled_sync(now + timedelta(hours=3)))  # 12 o'clock: new window

    def test_first_run_retries_after_failure(self):
        self.scan([], error=LDAPError("down"))
        now = datetime(2026, 10, 23, 10, 0)
        people.run_scheduled_sync(now)
        self.assertIsNone(people.run_scheduled_sync(now + timedelta(minutes=5)))
        self.assertIsNotNone(people.run_scheduled_sync(now + timedelta(minutes=16)))   # again after 15 minutes

    def test_without_service_account_nothing(self):
        set_setting("ldap_bind_dn", "")
        db.session.commit()
        self.assertIsNone(people.run_scheduled_sync())


class AccountTests(CacheBase):
    def test_existing_account_updated_without_login(self):
        u = self.ldap_user("berta", "Berta Alt", guid="g1")
        stats, _cls = self.sync([person("berta", "Berta Neu", guid="g1", email="b@neu.de")])
        db.session.refresh(u)
        self.assertEqual((u.display_name, u.email, [g.name for g in u.groups]), ("Berta Neu", "b@neu.de", ["IT"]))
        self.assertEqual(stats["accounts_updated"], 1)

    def test_group_removed_in_ad(self):
        u = self.ldap_user("berta", guid="g1")
        u.groups = [self.group]
        db.session.commit()
        self.sync([person("berta", "Berta", guid="g1", groups=())])
        db.session.refresh(u)
        self.assertEqual(u.groups, [])

    def test_rename_in_ad_keeps_account_and_loans(self):
        u = self.ldap_user("berta", guid="g1")
        db.session.add(Loan(book=self.book, user=u, due_at=date.today()))
        db.session.commit()
        stats, _cls = self.sync([person("berta.weill", "Berta Weill", guid="g1")])
        db.session.refresh(u)
        self.assertEqual(u.username, "berta.weill")
        self.assertEqual(stats["accounts_renamed"], 1)
        self.assertEqual(Loan.query.filter_by(user_id=u.id).count(), 1)
        self.assertEqual(User.query.filter_by(auth_source="ldap").count(), 1)

    def test_rename_skipped_when_name_taken(self):
        u = self.ldap_user("berta", guid="g1")
        self.sync([person("anna", "Anna", guid="g1")])          # "anna" is a local account
        db.session.refresh(u)
        self.assertEqual(u.username, "berta")
        self.assertEqual(User.query.filter_by(username="anna").one().auth_source, "local")

    def test_gone_account_disabled_and_back(self):
        u = self.ldap_user("berta", guid="g1")
        epoch = u.auth_epoch or 0
        for name in ("a1", "a2", "a3", "a4", "a5", "a6"):
            self.ldap_user(name, guid="g-" + name)
        others = [person(n, n, guid="g-" + n) for n in ("a1", "a2", "a3", "a4", "a5", "a6")]
        stats, _cls = self.sync(others)
        db.session.refresh(u)
        self.assertFalse(u.active)
        self.assertIsNotNone(u.ldap_gone_at)
        self.assertGreater(u.auth_epoch, epoch)                   # sessions ended
        self.assertEqual(stats["accounts_disabled"], 1)
        stats, _cls = self.sync(others + [person("berta", "Berta", guid="g1")])
        db.session.refresh(u)
        self.assertTrue(u.active)
        self.assertIsNone(u.ldap_gone_at)
        self.assertEqual(stats["accounts_reactivated"], 1)

    def test_manually_disabled_stays_disabled(self):
        u = self.ldap_user("berta", guid="g1", active=False)
        self.sync([person("berta", "Berta", guid="g1")])
        db.session.refresh(u)
        self.assertFalse(u.active)

    def test_safety_brake(self):
        for i in range(10):
            self.ldap_user(f"p{i}", guid=f"g{i}")
        stats, _cls = self.sync([person("p0", "P0", guid="g0")])  # 9 of 10 missing – more likely a misconfiguration
        self.assertEqual(stats["accounts_disabled"], 0)
        self.assertEqual(stats["accounts_brake"], 9)
        self.assertEqual(User.query.filter_by(auth_source="ldap", active=True).count(), 10)
        self.assertIn("Sicherheitsbremse", people.result_text(people.last_result()))

    def test_local_accounts_untouched(self):
        anna = User.query.filter_by(username="anna").one()
        self.sync([person("anna", "Anna AD", guid="gA")])
        db.session.refresh(anna)
        self.assertEqual((anna.auth_source, anna.display_name, anna.active), ("local", "Anna", True))

    def test_namesake_with_other_guid_not_taken_over(self):
        old = self.ldap_user("berta", guid="alt")
        db.session.add(Loan(book=self.book, user=old, due_at=date.today()))
        db.session.commit()
        self.directory(account=person("berta", "Berta Neu", guid="neu"))
        self.c.post(f"/buecher/{self.gbook.id}/ausleihen", data={"benutzer": "berta"})
        # find() returns the old account (same name) – only account_for recognises the other person
        from catalytix.views.main import account_for
        self.assertIsNone(account_for(person("berta", "Berta Neu", guid="neu")))
        db.session.refresh(old)
        self.assertFalse(old.active)
        self.assertNotEqual(old.username, "berta")
        self.assertEqual(Loan.query.filter_by(user_id=old.id).count(), 1)


class NamesakeTests(CacheBase):
    def test_sync_retires_old_account_and_frees_name(self):
        old = self.ldap_user("berta", guid="alt")
        db.session.add(Loan(book=self.book, user=old, due_at=date.today()))
        db.session.commit()
        stats, _cls = self.sync([person("berta", "Berta Neu", guid="neu")])
        db.session.refresh(old)
        self.assertFalse(old.active)
        self.assertEqual(old.username, f"berta~{old.id}")
        self.assertEqual(stats["accounts_disabled"], 1)
        self.assertEqual(Loan.query.filter_by(user_id=old.id).count(), 1)     # loan stays with the old person
        # at the desk "berta" is now the new person
        self.directory(account=person("berta", "Berta Neu", guid="neu"))
        self.c.post(f"/buecher/{self.gbook.id}/ausleihen", data={"benutzer": "berta"})
        new = User.query.filter_by(username="berta").one()
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.ldap_guid, "neu")

    def test_desk_recheck_retires_gone_namesake(self):
        old = self.ldap_user("berta", guid="alt", active=False)
        old.ldap_gone_at = datetime.now()
        db.session.commit()
        self.directory(account=person("berta", "Berta Neu", guid="neu"))
        cand = people.find("berta")
        self.assertTrue(getattr(cand, "is_candidate", False))
        db.session.refresh(old)
        self.assertNotEqual(old.username, "berta")

    def test_desk_recheck_reactivates_same_person(self):
        u = self.ldap_user("berta", guid="g1", active=False)
        u.ldap_gone_at = datetime.now()
        db.session.commit()
        self.directory(account=person("berta", "Berta", guid="g1"))
        self.assertEqual(people.find("berta").id, u.id)
        db.session.refresh(u)
        self.assertTrue(u.active)


def fillers(n=6):
    return [person(f"f{i}", f"F{i}", guid=f"gf{i}") for i in range(n)]


class ReviewFixTests(CacheBase):
    """Findings from the code review of 2.14.0."""

    def mkfill(self):
        for i in range(6):
            self.ldap_user(f"f{i}", guid=f"gf{i}")

    def rename_and_reuse(self, moved_first):
        self.mkfill()
        x = self.ldap_user("berta", guid="g1")
        moved, newp = person("berta.m", "Berta M", guid="g1"), person("berta", "Berta Neu", guid="g2")
        stats, _cls = self.sync(fillers() + ([moved, newp] if moved_first else [newp, moved]))
        db.session.refresh(x)
        return x, stats

    def check_rename_and_reuse(self, moved_first):
        x, stats = self.rename_and_reuse(moved_first)
        self.assertEqual((x.username, x.active, x.ldap_gone_at), ("berta.m", True, None))
        self.assertEqual(stats["accounts_disabled"], 0)

    def test_rename_plus_reuse_renamed_first(self):             # H2
        self.check_rename_and_reuse(True)

    def test_rename_plus_reuse_new_first(self):
        self.check_rename_and_reuse(False)

    def test_admin_locked_stays_locked_on_rename_and_reuse(self):   # H2
        self.mkfill()
        x = self.ldap_user("berta", guid="g1", active=False)
        lst = fillers() + [person("berta.m", "Berta M", guid="g1"), person("berta", "Berta Neu", guid="g2")]
        self.sync(lst)
        self.sync(lst)
        db.session.refresh(x)
        self.assertFalse(x.active)

    def test_retire_keeps_admin_lock(self):
        self.mkfill()
        x = self.ldap_user("berta", guid="g1", active=False)      # locked manually, person gone from AD
        self.sync(fillers() + [person("berta", "Berta Neu", guid="g2")])
        db.session.refresh(x)
        self.assertEqual((x.active, x.ldap_gone_at), (False, None))
        self.sync(fillers() + [person("berta", "Berta Neu", guid="g2"), person("berta.x", "B", guid="g1")])
        db.session.refresh(x)
        self.assertFalse(x.active)                               # comes back – still stays locked

    def test_login_of_namesake_moves_renamed_person(self):      # H2 (login)
        x = self.ldap_user("berta", guid="g1", active=False)    # locked manually
        self.c.post("/logout")
        cls = self.directory()
        cls.return_value.find_by_guid.return_value = person("berta.m", "Berta M", guid="g1")
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta Neu", guid="g2")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            r = self.c.post("/login", data={"username": "berta", "password": "x"})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        x = db.session.get(User, x.id)
        self.assertEqual((x.username, x.active), ("berta.m", False))      # moves, stays locked
        self.assertEqual(User.query.filter_by(username="berta").one().ldap_guid, "g2")

    def test_login_namesake_unresolvable_refused(self):
        x = self.ldap_user("berta", guid="g1")
        self.c.post("/logout")
        cls = self.directory()
        cls.return_value.find_by_guid.side_effect = LDAPError("down")
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta Neu", guid="g2")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            r = self.c.post("/login", data={"username": "berta", "password": "x"})
        self.assertEqual(r.status_code, 401)
        db.session.refresh(x)
        self.assertEqual((x.username, x.ldap_guid, x.active), ("berta", "g1", True))

    def test_duplicate_usernames_do_not_lock_legit_account(self):
        self.mkfill()
        x = self.ldap_user("berta", guid="g1")
        self.sync(fillers() + [person("berta", "Berta (Dom2)", guid="g9"), person("berta", "Berta", guid="g1")])
        db.session.refresh(x)
        self.assertEqual((x.username, x.active), ("berta", True))

    def test_retire_name_collision(self):
        self.mkfill()
        x = self.ldap_user("berta", guid="g1")
        db.session.add(User(username=f"berta~{x.id}", role="reader", auth_source="local"))
        db.session.commit()
        stats, _cls = self.sync(fillers() + [person("berta", "Berta Neu", guid="g2")])
        self.assertNotIn("error", stats)
        db.session.refresh(x)
        self.assertEqual(x.username, f"berta~{x.id}-2")

    def test_session_recheck_never_moves_account(self):         # H1
        from catalytix import __init__ as cx  # noqa: F401
        u = self.ldap_user("berta", guid="g1")
        u.set_password(PW)
        db.session.commit()
        self.c.post("/logout")
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta", guid="g1")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            self.c.post("/login", data={"username": "berta", "password": "x"})
        with self.c.session_transaction() as sess:
            sess["ldap_ok_at"] = 0                               # re-check due
        self.directory(account=person("berta", "Berta NEU", guid="g2", role="admin"))
        r = self.c.get("/")
        self.assertIn("/login", r.headers.get("Location", ""))  # session ended
        db.session.refresh(u)
        self.assertEqual((u.ldap_guid, u.role, u.display_name), ("g1", "reader", "Berta"))

    def test_uncertain_people_left_alone(self):                 # M1
        from catalytix.ldap_auth import ScanResult
        self.mkfill()
        carl = self.ldap_user("carl", guid="gc")
        self.sync(fillers() + [person("carl", "Carl", guid="gc")])
        res = ScanResult((p, "k-" + p.username, True) for p in fillers())
        res.uncertain.append(("carl", "gc"))
        cls = self.directory()
        cls.return_value.scan_people.return_value = res
        stats = people.sync_directory()
        db.session.refresh(carl)
        self.assertTrue(carl.active)
        self.assertEqual(DirectoryPerson.query.filter_by(username_key="carl").count(), 1)
        self.assertEqual(stats["uncertain"], 1)

    def test_lost_lock_aborts_and_keeps_other_lock(self):       # M3
        cls = self.directory()

        def scan(reuse, progress=None):
            set_setting("dircache_running", "2099-01-01T00:00:00|fremd")   # another process took over
            db.session.commit()
            progress(0, 1)
            return [(person("berta", "Berta"), "k", True)]
        cls.return_value.scan_people.side_effect = scan
        stats = people.sync_directory()
        self.assertIn("abgebrochen", stats["error"])
        self.assertEqual(get_setting("dircache_running"), "2099-01-01T00:00:00|fremd")
        self.assertEqual(DirectoryPerson.query.count(), 0)

    def test_cache_off_during_run_stores_nothing(self):
        cls = self.directory()

        def scan(reuse, progress=None):
            set_setting("ldap_cache_enabled", "0")
            db.session.commit()
            return [(person("zed", "Zed"), "k", True)]
        cls.return_value.scan_people.side_effect = scan
        people.sync_directory()
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertFalse(get_setting("dircache_ok_at"))

    def test_retry_after_error_within_window(self):
        self.scan([person("berta", "Berta")])
        now = datetime(2026, 10, 23, 9, 0)
        people.run_scheduled_sync(now)                          # first run ok
        cls = self.scan([], error=LDAPError("down"))
        people.run_scheduled_sync(now + timedelta(hours=1))     # new window, error
        self.assertIsNone(people.run_scheduled_sync(now + timedelta(hours=1, minutes=5)))
        cls.return_value.scan_people.side_effect = None
        cls.return_value.scan_people.return_value = [(person("berta", "Berta"), "k", True)]
        self.assertIsNotNone(people.run_scheduled_sync(now + timedelta(hours=1, minutes=16)))

    def test_stale_cache_not_used(self):
        self.sync([person("berta", "Berta")])
        set_setting("dircache_ok_at", (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"))
        db.session.commit()
        self.assertFalse(people.cache_ready())

    def test_mass_rename_brake(self):
        for i in range(10):
            self.ldap_user(f"p{i}", guid=f"g{i}")
        stats, _cls = self.sync([person(f"p{i}@x.de", f"P{i}", guid=f"g{i}") for i in range(10)])
        self.assertEqual(stats["rename_brake"], 10)
        self.assertEqual(User.query.filter(User.username.like("%@x.de")).count(), 0)

    def test_desk_recheck_throttled(self):
        u = self.ldap_user("berta", guid="g1", active=False)
        u.ldap_gone_at = datetime.now()
        db.session.commit()
        cls = self.directory(account=None)
        people.find("berta")
        people.find("berta")
        self.assertEqual(cls.return_value.check_account.call_count, 1)


class ReviewRound2Tests(CacheBase):
    """Follow-up check of the code review of 2.14.0."""

    def test_domain_migration_new_guids_braked(self):          # N1
        for i in range(10):
            self.ldap_user(f"p{i}", guid=f"alt{i}")
        stats, _cls = self.sync([person(f"p{i}", f"P{i}", guid=f"neu{i}") for i in range(10)])
        self.assertEqual(stats["accounts_brake"], 10)
        self.assertEqual(User.query.filter_by(auth_source="ldap", active=True).count(), 10)
        self.assertEqual(User.query.filter(User.username.like("%~%")).count(), 0)

    def test_find_by_guid_asks_objectguid_and_entryuuid(self):  # N2
        from catalytix.ldap_auth import Directory
        d = Directory({"ldap_server": "ldap://dc", "ldap_base_dn": "DC=x,DC=de", "ldap_bind_dn": "CN=svc"})
        conn = mock.Mock()
        conn.response, conn.result = [], {"result": 0}
        with mock.patch.object(d, "service_connection", return_value=conn):
            self.assertIsNone(d.find_by_guid("0123456789abcdef0123456789abcdef"))
        flt = conn.search.call_args[0][1]
        self.assertIn("(objectGUID=\\01\\23", flt)
        self.assertIn("(entryUUID=01234567-89ab-cdef-0123-456789abcdef)", flt)

    def test_partial_group_result_is_uncertain(self):           # N3
        from catalytix.ldap_auth import Directory, LDAPError as LE
        d = Directory({"ldap_server": "ldap://dc", "ldap_base_dn": "DC=x,DC=de", "ldap_bind_dn": "CN=svc"})
        conn = mock.Mock()

        def search(*a, **k):
            conn.response = [{"type": "searchResEntry", "dn": "CN=G,DC=x,DC=de"}]
            conn.result = {"result": 3, "description": "timeLimitExceeded"}
            return True                                         # ldap3: True despite partial result
        conn.search.side_effect = search
        d.strict = True
        with self.assertRaises(LE):
            d._search_dns(conn, "DC=x,DC=de", "(cn=*)")
        d.strict = False
        self.assertEqual(d._search_dns(conn, "DC=x,DC=de", "(cn=*)"), ["CN=G,DC=x,DC=de"])

    def test_blocking_departed_account_freed_at_login(self):    # N4
        renamed = self.ldap_user("berta", guid="g1")
        departed = self.ldap_user("berta.m", guid="g3", active=False)
        departed.ldap_gone_at = datetime.now()
        db.session.commit()
        self.c.post("/logout")
        cls = self.directory()
        cls.return_value.find_by_guid.side_effect = lambda g: person("berta.m", "Berta M", guid="g1") if g == "g1" else None
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta Neu", guid="g2")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            r = self.c.post("/login", data={"username": "berta", "password": "x"})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, renamed.id).username, "berta.m")
        self.assertTrue(db.session.get(User, departed.id).username.startswith("berta.m~"))
        self.assertEqual(User.query.filter_by(username="berta").one().ldap_guid, "g2")

    def test_unresolvable_login_not_counted_as_failure(self):   # N4
        self.ldap_user("berta", guid="g1")
        self.c.post("/logout")
        cls = self.directory()
        cls.return_value.find_by_guid.side_effect = LDAPError("down")
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta Neu", guid="g2")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True), \
                mock.patch("catalytix.lockout.fail") as fail:
            self.c.post("/login", data={"username": "berta", "password": "x"})
        fail.assert_not_called()

    def test_accounts_synced_even_without_cache(self):
        set_setting("ldap_cache_enabled", "0")
        db.session.commit()
        u = self.ldap_user("berta", "Alt", guid="g1")
        stats, _cls = self.sync([person("berta", "Berta Neu", guid="g1")])
        db.session.refresh(u)
        self.assertEqual(u.display_name, "Berta Neu")
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertFalse(stats["cache"])
        self.assertIn("Cache aus", people.result_text(people.last_result()))

    def test_without_cache_daily(self):
        set_setting("ldap_cache_enabled", "0")
        db.session.commit()
        self.scan([person("berta", "Berta")])
        now = datetime(2026, 10, 23, 9, 0)
        self.assertIsNotNone(people.run_scheduled_sync(now))
        self.assertIsNone(people.run_scheduled_sync(now + timedelta(hours=5)))
        self.assertIsNotNone(people.run_scheduled_sync(now + timedelta(days=1)))

    def test_desk_recheck_outage_pause(self):                   # L5
        for name in ("a", "b"):
            u = self.ldap_user(name, guid="g" + name, active=False)
            u.ldap_gone_at = datetime.now()
        db.session.commit()
        cls = self.directory(error=LDAPError("down"))
        people.find("a")
        people.find("b")
        self.assertEqual(cls.return_value.check_account.call_count, 1)


class LoginTests(CacheBase):
    def test_login_after_rename_uses_same_account(self):
        u = self.ldap_user("berta", guid="g1")
        self.c.post("/logout")
        info = person("berta.weill", "Berta Weill", guid="g1")
        with mock.patch("catalytix.views.main.authenticate", return_value=info), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            self.c.post("/login", data={"username": "berta.weill", "password": "x"})
        db.session.refresh(u)
        self.assertEqual(u.username, "berta.weill")
        self.assertEqual(User.query.filter_by(auth_source="ldap").count(), 1)

    def test_login_reactivates_gone_account(self):
        u = self.ldap_user("berta", guid="g1", active=False)
        u.ldap_gone_at = datetime.now()
        db.session.commit()
        self.c.post("/logout")
        with mock.patch("catalytix.views.main.authenticate", return_value=person("berta", "Berta", guid="g1")), \
                mock.patch("catalytix.views.main.ldap_enabled", return_value=True):
            r = self.c.post("/login", data={"username": "berta", "password": "x"})
        db.session.refresh(u)
        self.assertTrue(u.active)
        self.assertEqual(r.status_code, 302)


class AdminPageTests(CacheBase):
    def setUp(self):
        super().setUp()
        admin = User(username="admin", role="admin", auth_source="local")
        admin.set_password(PW)
        db.session.add(admin)
        db.session.commit()
        self.c.post("/logout")
        self.c.post("/login", data={"username": "admin", "password": PW})

    def test_page_shows_status(self):
        self.sync([person("berta", "Berta Brecht")])
        html = self.c.get("/admin/verzeichnisdienst").get_data(as_text=True)
        self.assertIn("Personen-Cache", html)
        self.assertIn("1 Person im Cache", html)

    def test_sync_now_runs_in_background(self):
        with mock.patch.object(people, "start_sync_async", return_value=True) as start:
            r = self.c.post("/admin/verzeichnisdienst", data={"action": "cache_sync"})
        start.assert_called_once()
        self.assertIn("#cache", r.headers["Location"])

    def test_clear_cache(self):
        self.sync([person("berta", "Berta Brecht")])
        self.c.post("/admin/verzeichnisdienst", data={"action": "cache_clear"})
        self.assertEqual(DirectoryPerson.query.count(), 0)

    def test_saving_with_cache_off_clears(self):
        self.sync([person("berta", "Berta Brecht")])
        data = {k: get_setting(k) for k in ("ldap_server", "ldap_bind_dn", "ldap_base_dn", "ldap_user_attr")}
        data.update(action="save", ldap_enabled="1", ldap_group_sync="1", ldap_cache_hours="2")
        self.c.post("/admin/verzeichnisdienst", data=data)            # "use cache" checkbox missing
        self.assertEqual(DirectoryPerson.query.count(), 0)
        self.assertEqual(get_setting("ldap_cache_hours"), "2")

    def test_invalid_hours_rejected(self):
        data = {"action": "save", "ldap_enabled": "1", "ldap_cache_enabled": "1", "ldap_cache_hours": "0",
                "ldap_bind_dn": "CN=svc,DC=x,DC=de"}
        self.c.post("/admin/verzeichnisdienst", data=data)
        self.assertEqual(get_setting("ldap_cache_hours"), "1")

    def test_gone_badge_in_user_list(self):
        u = self.ldap_user("berta", active=False)
        u.ldap_gone_at = datetime.now()
        db.session.commit()
        self.assertIn("nicht mehr im AD", self.c.get("/admin/benutzer").get_data(as_text=True))


class ScanPeopleTests(Base):
    """scan_people itself: complete read, reuse of the groups, no computers/disabled accounts."""

    def setUp(self):
        super().setUp()
        from catalytix import ldap_auth
        self.la = ldap_auth
        self.d = ldap_auth.Directory({"ldap_server": "ldap://dc", "ldap_base_dn": "DC=x,DC=de",
                                      "ldap_bind_dn": "CN=svc,DC=x,DC=de", "ldap_user_attr": "sAMAccountName"})
        self.entries = [{"type": "searchResEntry", "dn": "CN=Berta,DC=x,DC=de",
                         "raw_attributes": {"objectGUID": [bytes(range(16))]},
                         "attributes": {"sAMAccountName": "berta", "displayName": "Berta", "memberOf": [GDN],
                                        "userAccountControl": 512, "primaryGroupID": "513"}}]

    def conn(self, result=0):
        c = mock.Mock()
        c.extend.standard.paged_search.return_value = iter(self.entries)
        c.result = {"result": result, "description": "sizeLimitExceeded" if result else "success"}
        return c

    def test_complete_scan_with_guid(self):
        with mock.patch.object(self.d, "service_connection", return_value=self.conn()), \
                mock.patch.object(self.d, "user_groups", return_value={GDN: "direkt"}), \
                mock.patch.object(self.la, "resolve_role", return_value=("reader", "x")):
            found = self.d.scan_people()
        info, key, fresh = found[0]
        self.assertEqual((info.username, info.guid, fresh), ("berta", bytes(range(16)).hex(), True))

    def test_incomplete_scan_raises(self):
        with mock.patch.object(self.d, "service_connection", return_value=self.conn(result=4)):
            with self.assertRaises(LDAPError):
                self.d.scan_people()

    def test_known_groups_reused(self):
        with mock.patch.object(self.d, "service_connection", return_value=self.conn()), \
                mock.patch.object(self.d, "user_groups") as ug, \
                mock.patch.object(self.la, "resolve_role", return_value=("reader", "x")):
            found = self.d.scan_people(reuse=lambda dn, key: [GDN])
        ug.assert_not_called()
        self.assertFalse(found[0][2])
        self.assertEqual(found[0][0].groups, [GDN])

    def test_filter_excludes_computers_and_disabled(self):
        c = self.conn()
        with mock.patch.object(self.d, "service_connection", return_value=c), \
                mock.patch.object(self.d, "user_groups", return_value={}), \
                mock.patch.object(self.la, "resolve_role", return_value=(None, "")):
            self.assertEqual(self.d.scan_people(), [])             # without a role not in the cache
        flt = c.extend.standard.paged_search.call_args[0][1]
        self.assertIn("(objectCategory=person)", flt)
        self.assertIn("(!(userAccountControl:1.2.840.113556.1.4.803:=2))", flt)

    def test_sync_reuses_groups_for_unchanged_people(self):
        """Full flow: a second sync without changes does not resolve any groups again."""
        set_setting("ldap_server", "ldap://dc")
        set_setting("ldap_base_dn", "DC=x,DC=de")
        db.session.commit()
        from catalytix import people as ppl
        ppl._cache.clear()
        calls = []
        with mock.patch.object(self.la.Directory, "service_connection", side_effect=lambda *a: self.conn()), \
                mock.patch.object(self.la.Directory, "user_groups",
                                  side_effect=lambda *a: calls.append(1) or {GDN: "direkt"}), \
                mock.patch.object(self.la, "resolve_role", return_value=("reader", "x")):
            ppl.sync_directory()
            ppl.sync_directory()
        self.assertEqual(len(calls), 1)
        self.assertEqual(DirectoryPerson.query.one().guid, bytes(range(16)).hex())


if __name__ == "__main__":
    unittest.main()


class DirectBindIdentityTests(Base):
    """Without a service account: the UPN bind must not count as proof for another entry with the same login name."""

    def test_password_checked_against_found_entry(self):
        from catalytix.ldap_auth import Directory
        d = Directory({"ldap_server": "ldap://dc", "ldap_base_dn": "DC=x,DC=de", "ldap_bind_dn": "",
                       "ldap_upn_suffix": "x.de", "ldap_user_attr": "sAMAccountName"})
        victim = {"type": "searchResEntry", "dn": "CN=Opfer,DC=x,DC=de",
                  "attributes": {"sAMAccountName": "chef", "userAccountControl": 512}}
        binds = []

        def connect(user, password):
            binds.append(user)
            if user == "CN=Opfer,DC=x,DC=de":      # the attacker's password does not match the victim
                raise LDAPError("Bind abgelehnt")
            return mock.Mock()
        with mock.patch.object(d, "connect", side_effect=connect), \
                mock.patch.object(d, "find_user", return_value=victim):
            with self.assertRaises(LDAPError):
                d.authenticate("chef", "angreifer-passwort")   # UPN chef@x.de belongs to the attacker
        self.assertEqual(binds, ["chef@x.de", "CN=Opfer,DC=x,DC=de"])
