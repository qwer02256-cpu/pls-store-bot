"""Verify exported series metadata and the new navigation/media transitions."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import db, banners
from services.licenses import issue
from handlers import licenses, marketing
import main

ROOT = Path(__file__).resolve().parents[2]


class SeriesExportTests(unittest.TestCase):
    def test_complete_export_matches_verified_shared_brand_and_all_declared_screens(self):
        design = ROOT / 'design/banners'
        spec = json.loads((design / 'series.json').read_text())
        self.assertEqual({item['key'] for item in spec}, set(banners.BANNER_KEYS))
        self.assertEqual(len(spec), 21)
        brand_hash = hashlib.sha256((design / 'brand-lockup.png').read_bytes()).hexdigest()
        for lang in ('ru', 'en', 'uk'):
            report = json.loads((design / f'build-report-{lang}.json').read_text())
            self.assertEqual(len(report['banners']), 21)
            self.assertEqual(report['brand_position'], [1048, 24])
            self.assertEqual(report['brand_sha256'], brand_hash)
            for row in report['banners']:
                data = (ROOT / row['file']).read_bytes()
                self.assertEqual(struct.unpack('>II', data[16:24]), (1600, 900))
                self.assertEqual(hashlib.sha256(data).hexdigest(), row['sha256'])
                self.assertTrue(row['brand_layer_verified'])


class ExpandedNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(db, 'DB_PATH', Path(self.temp.name)/'store.db')
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(42)
        main.accept_license(42)
        self.actor = SimpleNamespace(id=42, username='buyer', first_name='Buyer')
        self.call = SimpleNamespace(from_user=self.actor, data='licenses', answer=AsyncMock())

    async def test_license_list_and_details_use_distinct_localized_banners(self):
        with db.db() as conn:
            pid = conn.execute("INSERT INTO products(name,price,description) VALUES('Plugin','349','Details')").lastrowid
            lid = issue(conn, 42, pid)
        for lang in ('ru', 'en', 'uk'):
            db.set_user_language(42, lang)
            with patch('services.screens.screen_reply', new_callable=AsyncMock) as render:
                await licenses.show_list(self.call, 42)
                self.assertEqual(render.call_args.kwargs['photo'], banners.banner_path('licenses', lang))
                self.call.data = f'lic:view:{lid}'
                await licenses.license_action(self.call, SimpleNamespace())
                self.assertEqual(render.call_args.kwargs['photo'], banners.banner_path('license_detail', lang))

    async def test_promo_and_api_have_own_images_and_admin_can_manage_every_banner(self):
        state = SimpleNamespace(set_state=AsyncMock())
        with patch.object(main, 'screen_reply', new_callable=AsyncMock) as render:
            await main.cb_promo(self.call, state)
            self.assertEqual(render.call_args.kwargs['photo'], main.banner_path('promo'))
        with patch.object(main, 'safe_edit_or_send', new_callable=AsyncMock) as render:
            await main.cb_api_info(self.call)
            self.assertEqual(render.call_args.kwargs['photo'], main.banner_path('api'))
        self.call.data = 'adm:banners:ru'
        with patch.object(main, 'ADMIN_ID', 42), patch.object(main, 'safe_edit_or_send', new_callable=AsyncMock) as render:
            await main.adm_banners(self.call)
            callbacks = [b.callback_data for row in render.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
            for key in banners.BANNER_KEYS:
                self.assertIn(f'adm:ban:{key}:ru', callbacks)

    def test_series_migration_backs_up_localized_uploads_and_only_runs_once(self):
        with db.db() as conn:
            conn.execute("DELETE FROM bot_settings WHERE key='banner_series_right_63_v1'")
        db.set_setting('banner_licenses_uk', 'old-upload.png')
        db.set_setting('support_username', 'owner')
        db.init_db()
        self.assertEqual(db.get_setting('banner_licenses_uk'), '')
        self.assertEqual(db.get_setting('banner_licenses_uk_before_right_63_v1'), 'old-upload.png')
        db.set_setting('banner_licenses_uk', 'new-upload.png')
        db.init_db()
        self.assertEqual(db.get_setting('banner_licenses_uk'), 'new-upload.png')
        self.assertEqual(db.get_setting('support_username'), 'owner')
