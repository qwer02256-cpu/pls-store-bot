import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from handlers import licenses
from services import db, license_files
from services.licenses import issue, owned
from services.product_assets import (
    source_version,
    normalize_source,
    publish_file,
    safe_filename,
)
from services.license_setup import setup_autostars
import main


class FakeState:
    def __init__(self, **data):
        self.data = data
        self.current = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.current = state

    async def clear(self):
        self.data = {}
        self.current = None


class ProductAssetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for owner, name, value in [
            (db, "DB_PATH", self.root / "db"),
            (licenses, "DB_PATH", self.root / "db"),
            (licenses, "ADMIN_ID", 1),
        ]:
            p = patch.object(owner, name, value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10)
        self.old = self.root / "Original.py"
        self.old.write_text("VERSION='1.0'\n# old")
        with db.db() as conn:
            self.pid = conn.execute(
                "INSERT INTO products(name,price,description,version) VALUES('Plugin','349','description','1.0')"
            ).lastrowid
            conn.execute(
                "INSERT INTO license_assets VALUES(?,?,?)",
                (self.pid, str(self.old), "Old functional guide"),
            )
            self.lid = issue(conn, 10, self.pid)
        self.key = owned(self.lid, 10)["license_key"]

    def message(self, text=None, name="MyPlugin.py"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            text=text,
            document=(
                SimpleNamespace(file_name=name, file_size=100) if text is None else None
            ),
            answer=AsyncMock(),
        )

    def data(self):
        with db.db() as conn:
            return dict(
                conn.execute(
                    "SELECT a.*,p.version FROM license_assets a JOIN products p ON p.id=a.product_id WHERE a.product_id=?",
                    (self.pid,),
                ).fetchone()
            )

    async def upload(self, source, name="MyPlugin.py"):
        state = FakeState(pid=self.pid, kind="file")

        async def download(doc, destination):
            Path(destination).write_text(source)

        with patch.object(licenses, "screen_reply", AsyncMock()):
            await licenses.upload(
                self.message(name=name),
                state,
                SimpleNamespace(download=AsyncMock(side_effect=download)),
            )
        return state

    async def test_upload_updates_filename_and_version_and_old_license_downloads_new_file(
        self,
    ):
        source = "VERSION='2.4.1'\n# Updated feature"
        await self.upload(source)
        asset = self.data()
        self.assertEqual(asset["version"], "2.4.1")
        self.assertEqual(Path(asset["file_path"]).name, "MyPlugin.py")
        self.assertEqual(Path(asset["file_path"]).read_text(), source)
        self.assertTrue(self.old.exists())
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        await licenses.deliver(bot, self.lid, 10)
        docs = [c.args[1] for c in bot.send_document.await_args_list]
        self.assertEqual(
            [d.filename for d in docs],
            ["MyPlugin.py", "LICENSE_KEY.txt", "INSTALL.txt"],
        )
        self.assertEqual(docs[0].data.decode(), source)
        self.assertIn(self.key, docs[1].data.decode())
        self.assertIn("2.4.1", docs[1].data.decode())
        self.assertEqual(docs[2].data.decode(), "Old functional guide")
        self.assertEqual(owned(self.lid, 10)["license_key"], self.key)
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM licenses").fetchone()[0], 1
            )

    async def test_versionless_file_is_not_published_until_version_is_supplied(self):
        state = await self.upload("# new version without metadata")
        self.assertEqual(state.current, licenses.Upload.version)
        self.assertEqual(self.data()["file_path"], str(self.old))
        with patch.object(licenses, "screen_reply", AsyncMock()):
            await licenses.asset_version(self.message(text="2.5-beta"), state)
        self.assertEqual(self.data()["version"], "2.5-beta")
        self.assertNotEqual(self.data()["file_path"], str(self.old))

    async def test_invalid_python_cannot_replace_current_file(self):
        await self.upload("def broken(")
        self.assertEqual(self.data()["file_path"], str(self.old))
        self.assertEqual(self.data()["version"], "1.0")

    def test_metadata_is_read_without_executing_calls(self):
        source = "VERSION=__import__('os').system('never run')\nraise RuntimeError('do not execute')"
        self.assertIsNone(source_version(source))
        self.assertEqual(source_version("VERSION: str = '3.0'"), "3.0")
        self.assertEqual(source_version("VERSION='v3.1-rc1'"), "v3.1-rc1")

    def test_unsafe_filename_is_rejected(self):
        for name in ("../bad.py", "a/b.py", "a\\b.py", "plugin.txt", "bad\n.py"):
            with self.assertRaises(ValueError):
                safe_filename(name)

    def test_autostars_downloaded_endpoint_is_normalized_and_license_guard_required(
        self,
    ):
        source = '# PLS_LICENSE_GUARD_V1\nVERSION="2.3"\n_PLS_ENDPOINT="https://old.example/licenses/verify"\n'
        normalized = normalize_source(source, True)
        self.assertIn('"__PLS_LICENSE_ENDPOINT__"', normalized)
        self.assertNotIn("old.example", normalized)
        self.assertEqual(source_version(normalized), "2.3")
        with self.assertRaises(ValueError):
            normalize_source("VERSION='2.3'", True)

    async def test_autostars_product_cannot_bypass_guard_by_renaming_upload(self):
        with db.db() as conn:
            conn.execute("UPDATE products SET name='AutoStars' WHERE id=?", (self.pid,))
        await self.upload("VERSION='3.0'", name="Renamed.py")
        self.assertEqual(self.data()["file_path"], str(self.old))

    def test_failed_publication_rolls_back_file_pointer_and_version(self):
        new = self.root / "New.py"
        new.write_text("VERSION='2.0'")
        with db.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_version BEFORE UPDATE OF version ON products BEGIN SELECT RAISE(ABORT,'failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            publish_file(self.pid, new, "2.0", 1)
        self.assertEqual(
            (self.data()["file_path"], self.data()["version"]), (str(self.old), "1.0")
        )

    async def test_catalog_buttons_start_file_and_guide_inputs(self):
        call = SimpleNamespace(
            data=f"prodasset:file:{self.pid}",
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        state = FakeState()
        with patch.object(licenses, "screen_reply", AsyncMock()):
            await licenses.product_assets_action(call, state)
        self.assertEqual(state.current, licenses.Upload.content)
        self.assertEqual(state.data, {"pid": self.pid, "kind": "file"})
        call.data = f"prodasset:guide:{self.pid}"
        with patch.object(licenses, "screen_reply", AsyncMock()):
            await licenses.product_assets_action(call, state)
        self.assertEqual(state.data["kind"], "guide")

    async def test_non_admin_cannot_start_update_or_upload(self):
        call = SimpleNamespace(
            data=f"prodasset:file:{self.pid}",
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        state = FakeState()
        await licenses.product_assets_action(call, state)
        self.assertIsNone(state.current)
        message = self.message()
        message.from_user.id = 10
        bot = SimpleNamespace(download=AsyncMock())
        await licenses.upload(message, state, bot)
        bot.download.assert_not_awaited()

    async def test_txt_guide_upload_and_preview_use_utf8(self):
        text = "Подробная настройка функций: лоты, баланс, возвраты."
        state = FakeState(pid=self.pid, kind="guide")
        message = self.message(name="INSTALL.txt")
        bot = SimpleNamespace(
            download=AsyncMock(return_value=io.BytesIO(b"\xef\xbb\xbf" + text.encode()))
        )
        with patch.object(licenses, "screen_reply", AsyncMock()):
            await licenses.upload(message, state, bot)
        self.assertEqual(self.data()["instruction"], text)
        self.assertEqual(self.data()["file_path"], str(self.old))

    def test_manual_builtin_catalog_version_survives_restart_and_custom_guide_preserved(
        self,
    ):
        setup_autostars()
        with db.db() as conn:
            row = conn.execute(
                "SELECT id FROM products WHERE name='AutoStars'"
            ).fetchone()
            conn.execute(
                "UPDATE products SET version='2.9-manual' WHERE id=?", (row["id"],)
            )
            conn.execute(
                "UPDATE license_assets SET instruction='Custom operator instructions' WHERE product_id=?",
                (row["id"],),
            )
        setup_autostars()
        with db.db() as conn:
            row = conn.execute(
                "SELECT p.version,a.instruction FROM products p JOIN license_assets a ON a.product_id=p.id WHERE p.name='AutoStars'"
            ).fetchone()
            self.assertEqual(
                (row["version"], row["instruction"]),
                ("2.9-manual", "Custom operator instructions"),
            )

    def test_bundled_functional_guide_covers_actual_features_and_fits_upload_limit(
        self,
    ):
        guide = (
            Path(__file__).resolve().parents[1] / "docs/AUTOSTARS_INSTALL.txt"
        ).read_text()
        for heading in (
            "ПОДКЛЮЧЕНИЕ FRAGMENT",
            "СОЗДАНИЕ И ПРИВЯЗКА ЛОТОВ",
            "ВОЗВРАТЫ",
            "ЗАЩИТА ПО ОСТАТКУ TON",
            "ФИНАНСЫ И РАСЧЁТ",
            "ШАБЛОНЫ СООБЩЕНИЙ",
            "ПРЕДПРОДАЖНЫЙ",
            "ПРОКСИ",
            "ПЕРВЫЙ ПРОВЕРОЧНЫЙ ЗАКАЗ",
        ):
            self.assertIn(heading, guide)
        self.assertGreater(len(guide), 15000)
        self.assertLess(len(guide.encode()), 100000)
