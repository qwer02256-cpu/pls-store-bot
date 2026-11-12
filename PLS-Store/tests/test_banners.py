import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace

import main


class BannerTests(unittest.TestCase):
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
