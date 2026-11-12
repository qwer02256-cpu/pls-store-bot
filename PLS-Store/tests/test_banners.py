import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace

import main
from services import db


class BannerTests(unittest.TestCase):
    def test_series_upgrade_backs_up_overrides_once_and_keeps_later_uploads(self):
        with TemporaryDirectory() as tmp, patch.object(db, "DB_PATH", Path(tmp) / "store.db"):
            db.init_db()
            with db.db() as conn:
                conn.execute("DELETE FROM bot_settings WHERE key='banner_series_2026_11_applied'")
            db.set_setting("banner_home", "old-home.jpg")
            db.set_setting("banner_faq", "old-faq.jpg")
            db.set_setting("support_username", "owner_support")
            db.init_db()
            self.assertEqual(db.get_setting("banner_home"), "")
            self.assertEqual(db.get_setting("banner_faq"), "")
            self.assertEqual(db.get_setting("banner_home_before_series_2026_11"), "old-home.jpg")
            self.assertEqual(db.get_setting("banner_faq_before_series_2026_11"), "old-faq.jpg")
            self.assertEqual(db.get_setting("support_username"), "owner_support")
            db.set_setting("banner_home", "new-owner-upload.jpg")
            db.init_db()
            self.assertEqual(db.get_setting("banner_home"), "new-owner-upload.jpg")
            self.assertEqual(db.get_setting("banner_home_before_series_2026_11"), "old-home.jpg")

    def test_every_section_has_a_bundled_banner(self):
        with patch.object(main, "get_setting", return_value=""):
            for key in main.BANNER_KEYS:
                with self.subTest(section=key):
                    path = main.banner_path(key)
                    self.assertIsNotNone(path)
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_uploaded_banner_overrides_bundle_and_missing_upload_falls_back(self):
        with TemporaryDirectory() as tmp:
            upload = Path(tmp) / "owner.png"
            upload.write_bytes(b"owner upload")
            with patch.object(main, "BANNERS_DIR", Path(tmp)):
                with patch.object(main, "get_setting", return_value=upload.name):
                    self.assertEqual(main.banner_path("documents"), upload)
                with patch.object(main, "get_setting", return_value="missing.png"):
                    self.assertEqual(main.banner_path("documents"), main.ASSETS_DIR / "documents.png")


class LongBannerCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_history_is_sent_in_full_outside_photo_caption(self):
        message = SimpleNamespace(answer_photo=AsyncMock(), answer=AsyncMock())
        target = SimpleNamespace(message=message)
        history = "Покупка\n" * 180
        keyboard = object()
        await main.safe_edit_or_send(target, history, reply_markup=keyboard,
                                     photo=main.ASSETS_DIR / "purchases.png")
        message.answer_photo.assert_awaited_once()
        self.assertNotIn("caption", message.answer_photo.call_args.kwargs)
        self.assertEqual(message.answer.call_args.args[0], history)
        self.assertIs(message.answer.call_args.kwargs["reply_markup"], keyboard)
