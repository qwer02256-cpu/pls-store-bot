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
            for lang in ("ru", "en", "uk"):
                for key in main.BANNER_KEYS:
                    with self.subTest(section=key, language=lang):
                        path = main.banner_path(key, lang)
                        self.assertIsNotNone(path)
                        self.assertTrue(path.is_file())
                        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                        if lang != "ru":
                            self.assertEqual(path.parent, main.ASSETS_DIR / lang)

    def test_uploaded_banner_overrides_bundle_and_missing_upload_falls_back(self):
        with TemporaryDirectory() as tmp:
            upload = Path(tmp) / "owner.png"
            upload.write_bytes(b"owner upload")
            with patch.object(main, "BANNERS_DIR", Path(tmp)):
                with patch.object(main, "get_setting", return_value=upload.name):
                    self.assertEqual(main.banner_path("documents"), upload)
                with patch.object(main, "get_setting", return_value="missing.png"):
                    self.assertEqual(main.banner_path("documents"), main.ASSETS_DIR / "documents.png")

    def test_uploads_are_scoped_to_their_language(self):
        with TemporaryDirectory() as tmp:
            for name in ("russian.png", "english.png"):
                (Path(tmp) / name).touch()
            settings = {"banner_documents": "russian.png", "banner_documents_en": "english.png"}
            with patch.object(main, "BANNERS_DIR", Path(tmp)), patch.object(
                main, "get_setting", side_effect=lambda key, default="": settings.get(key, default)
            ):
                self.assertEqual(main.banner_path("documents", "ru"), Path(tmp) / "russian.png")
                self.assertEqual(main.banner_path("documents", "en"), Path(tmp) / "english.png")
                self.assertEqual(main.banner_path("documents", "uk"), main.ASSETS_DIR / "uk/documents.png")
                self.assertEqual(main.banner_path("documents", "invalid"), Path(tmp) / "russian.png")

    def test_missing_translation_falls_back_to_existing_russian_banner(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "documents.png").touch()
            with patch.object(main, "ASSETS_DIR", Path(tmp)), patch.object(main, "get_setting", return_value=""):
                self.assertEqual(main.banner_path("documents", "en"), Path(tmp) / "documents.png")


class LocalizedScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_choice_immediately_updates_home_and_all_section_banners(self):
        with TemporaryDirectory() as tmp, patch.object(db, "DB_PATH", Path(tmp) / "store.db"):
            db.init_db()
            actor = SimpleNamespace(id=42, username="buyer", first_name="Buyer")
            callback = SimpleNamespace(from_user=actor, answer=AsyncMock())
            bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="test_bot")))
            for lang in ("en", "uk", "ru"):
                callback.data = f"lang:set:{lang}"
                with patch.object(main, "safe_edit_or_send", AsyncMock()) as render:
                    await main.cb_language_set(callback)
                    self.assertEqual(db.get_user_language(actor.id), lang)
                    self.assertEqual(render.call_args.kwargs["photo"], main.banner_path("home", lang))
                    actions = [
                        ("shop", lambda: main.render_catalog(callback)),
                        ("plugins", lambda: main.render_plugins(callback)),
                        ("documents", lambda: main.show_license_gate(callback, actor.id)),
                        ("profile", lambda: main.cb_profile(callback)),
                        ("purchases", lambda: main.cb_mypurchases(callback)),
                        ("referral", lambda: main.cb_referral(callback, bot)),
                        ("topup", lambda: main.cb_topup(callback)),
                        ("payment", lambda: main.show_payment_methods(callback, actor, 1)),
                        ("language", lambda: main.cb_language(callback)),
                        ("faq", lambda: main.cb_faq(callback)),
                        ("documents", lambda: main.cb_documents(callback)),
                        ("support", lambda: main.cb_support(callback)),
                        ("reviews", lambda: main.cb_reviews(callback)),
                    ]
                    main.accept_license(actor.id)
                    for section, action in actions:
                        with self.subTest(section=section, language=lang):
                            render.reset_mock()
                            await action()
                            self.assertEqual(render.call_args.kwargs["photo"], main.banner_path(section, lang))
                db.init_db()
                self.assertEqual(db.get_user_language(actor.id), lang)

    async def test_admin_upload_targets_only_selected_language(self):
        state = SimpleNamespace(get_data=AsyncMock(return_value={"banner_key": "home", "banner_lang": "en"}),
                                clear=AsyncMock())
        message = SimpleNamespace(from_user=SimpleNamespace(id=42),
                                  photo=[SimpleNamespace(file_unique_id="image")], answer=AsyncMock())
        bot = SimpleNamespace(download=AsyncMock())
        with patch.object(main, "ADMIN_ID", 42), patch.object(main, "set_setting") as save:
            await main.adm_banner_save(message, bot, state)
            save.assert_called_once_with("banner_home_en", "home_en_image.jpg")


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
