"""3.1.0: formal address ("Sie") – default texts stored in the old "du" form are shown in the new form."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CATALYTIX_DATA_DIR", tempfile.mkdtemp(prefix="catalytix-test-"))

from test_app import BaseCase  # noqa: E402

from catalytix import branding  # noqa: E402
from catalytix.models import DEFAULT_SETTINGS, db, set_setting  # noqa: E402


class PreviousDefaultTests(BaseCase):
    def test_stored_old_login_text_shows_new_default(self):
        old = branding.PREVIOUS_DEFAULTS["login_text"][0]
        set_setting("login_text", old)
        db.session.commit()
        with self.app.test_request_context("/"):
            self.assertEqual(branding._default_text("login_text"), DEFAULT_SETTINGS["login_text"])
        self.assertIn("Ihrer Bibliothek", DEFAULT_SETTINGS["login_text"])

    def test_new_default_is_translated(self):
        from pathlib import Path
        en = json.loads((Path(branding.__file__).parent / "translations" / "en.json").read_text(encoding="utf-8"))
        self.assertIn(DEFAULT_SETTINGS["login_text"], en)

    def test_custom_text_is_kept(self):
        set_setting("login_text", "Willkommen in der Institutsbibliothek.")
        db.session.commit()
        with self.app.test_request_context("/"):
            self.assertEqual(branding._default_text("login_text"), "Willkommen in der Institutsbibliothek.")


if __name__ == "__main__":
    unittest.main()
