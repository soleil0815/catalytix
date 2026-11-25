"""3.5.2: the three newest "before update" backups are kept in addition to the set number for 30 days only (as in
AssetLotse 2.10.0); afterwards they rotate like all others, so deleted data does not stay in them for ever. The backup
page counts them separately."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import backup  # noqa: E402
from catalytix.models import db, set_setting  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0)


class UpdateBackupAgeTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.dir = backup.backup_dir()
        for f in self.dir.glob("catalytix-*.db"):
            f.unlink()

    def tearDown(self):
        for f in self.dir.glob("catalytix-*.db"):
            f.unlink()
        super().tearDown()

    def put(self, when: datetime, kind: str = "") -> str:
        name = f"catalytix-{when:%Y-%m-%d_%H%M%S}{kind}.db"
        (self.dir / name).write_bytes(b"x")
        return name

    def names(self) -> set[str]:
        return {b["name"] for b in backup.list_backups()}

    def test_old_update_backups_rotate_like_the_others(self):
        daily = [self.put(NOW - timedelta(days=d)) for d in range(1, 6)]
        old = [self.put(NOW - timedelta(days=d), f"-vor-3.{d}.0") for d in (40, 90, 250)]
        backup.rotate(keep=3, now=NOW)
        self.assertEqual(self.names(), set(daily[:3]))            # the set number, nothing more
        for name in old:
            self.assertNotIn(name, self.names())

    def test_young_update_backups_kept_in_addition(self):
        daily = [self.put(NOW - timedelta(days=d)) for d in range(1, 6)]
        young = [self.put(NOW - timedelta(days=d), f"-vor-3.{d}.0") for d in (2, 10, 29)]
        backup.rotate(keep=3, now=NOW)
        self.assertEqual(self.names(), set(daily[:3]) | set(young))

    def test_only_the_newest_three_are_protected(self):
        young = [self.put(NOW - timedelta(days=d), f"-vor-3.{d}.0") for d in (1, 2, 3, 4)]
        daily = [self.put(NOW - timedelta(days=d, hours=1)) for d in (5, 6)]
        backup.rotate(keep=2, now=NOW)
        # the fourth is not protected and competes with the daily ones (it is the newest of those)
        self.assertEqual(self.names(), set(young) | {daily[0]})

    def test_default_now_is_today(self):
        old = self.put(datetime.now() - timedelta(days=31), "-vor-3.0.0")
        young = self.put(datetime.now() - timedelta(days=1), "-vor-3.1.0")
        daily = self.put(datetime.now() - timedelta(hours=2))
        backup.rotate(keep=1)
        self.assertEqual(self.names(), {young, daily})
        self.assertNotIn(old, self.names())

    def test_backup_page_counts_separately(self):
        self.make_user("admin", "admin")
        self.login("admin")
        set_setting("backup_keep", "2")
        db.session.commit()
        now = datetime.now()
        for d in (1, 2):
            self.put(now - timedelta(days=d))
        self.put(now - timedelta(hours=3), "-vor-3.5.1")
        with mock.patch.object(backup, "supported", return_value=True):     # the tests use an in-memory database
            page = self.c.get("/admin/datensicherung").get_data(as_text=True)
        count = page.split("data-backup-count>", 1)[1].split("</span>", 1)[0]
        self.assertIn("2 von höchstens 2", count)
        self.assertIn("dazu 1 Sicherung vor einem Update (bleibt bis zu 30 Tage)", count)
        self.assertIn("bis zu 30 Tage", page.split('id="f-backup-keep"', 1)[1])

    def test_help_states_the_30_days(self):
        self.make_user("admin", "admin")
        self.login("admin")
        with mock.patch.object(backup, "supported", return_value=True):
            page = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("Sicherungen vor einem Update höchstens 30 Tage", page)       # privacy
        self.assertIn("30 Tage lang erhalten", page)                                 # backups


if __name__ == "__main__":
    unittest.main()
