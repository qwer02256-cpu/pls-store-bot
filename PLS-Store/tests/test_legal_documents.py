import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import httpx
import main
from services import db
from services import documents
from services.documents import (
    DOCUMENTS,
    KEYS,
    REVISION,
    document_url,
    setup_documents,
    document_pages,
    document_nodes,
)
from services.legal_publication import publish_documents
from services.license_setup import setup_autostars


class LegalDocumentsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.tmp.name) / "store.db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(42)
        setup_documents()
        self.created = {}
        self.calls = []
        self.token = "dummy-legal-private-token"

    def client(self, lose_first=False, change_license=False):
        async def handle(request):
            fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            method = request.url.path
            self.calls.append(method)
            if method == "/createAccount":
                result = {"access_token": self.token}
            elif method == "/getPageList":
                result = {
                    "total_count": len(self.created),
                    "pages": list(self.created.values()),
                }
            elif method == "/createPage":
                key = next(
                    k
                    for k in KEYS
                    if fields["title"] == DOCUMENTS[k]["title"] + " · " + REVISION
                )
                self.assertEqual(json.loads(fields["content"]), document_nodes(key))
                self.assertNotIn(self.token, str(request.url))
                if key in self.created:
                    self.fail("Duplicate legal page")
                result = {
                    "title": fields["title"],
                    "url": f"https://telegra.ph/PLS-{key}-11-13",
                }
                self.created[key] = result
                if change_license and key == "license":
                    db.set_setting(
                        "document_license_url", "https://example.org/operator-license"
                    )
                if lose_first and len(self.created) == 1:
                    raise httpx.ReadTimeout("response lost")
            else:
                self.fail("Unexpected Telegraph API request")
            return httpx.Response(200, json={"ok": True, "result": result})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def test_four_current_documents_publish_and_restart_does_not_duplicate(self):
        async with self.client() as client:
            await publish_documents(client)
            before = list(self.calls)
            await publish_documents(client)
        self.assertEqual(set(self.created), set(KEYS))
        self.assertEqual(self.calls, before)
        for key in KEYS:
            self.assertEqual(document_url(key), f"https://telegra.ph/PLS-{key}-11-13")
        self.assertEqual(db.get_setting("license_url"), document_url("license"))

    async def test_lost_response_recovers_page_and_finishes_other_documents(self):
        async with self.client(lose_first=True) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await publish_documents(client)
        async with self.client() as client:
            await publish_documents(client)
        self.assertEqual(set(self.created), set(KEYS))
        self.assertEqual(self.calls.count("/createAccount"), 1)
        self.assertEqual(self.calls.count("/createPage"), 4)

    async def test_admin_link_changed_during_publication_is_preserved(self):
        async with self.client(change_license=True) as client:
            await publish_documents(client)
        self.assertEqual(
            document_url("license"), "https://example.org/operator-license"
        )
        setup_documents()
        self.assertEqual(
            document_url("license"), "https://example.org/operator-license"
        )

    async def test_public_routes_serve_current_documents_and_cross_links(self):
        app = web.Application()
        documents.add_document_routes(app)
        async with TestClient(TestServer(app)) as client:
            for key in KEYS:
                response = await client.get("/documents/" + key)
                self.assertEqual(response.status, 200)
                body = await response.text()
                self.assertIn(DOCUMENTS[key]["title"], body)
                for other in KEYS:
                    self.assertIn(
                        documents.canonical_url(other).replace("&", "&amp;"), body
                    )
                self.assertIn("Лицензионное соглашение", body)
                self.assertIn("2026", body)
            self.assertEqual((await client.get("/documents/private")).status, 404)

    async def test_deep_link_reads_license_before_accepting_terms(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42, username="buyer", first_name="Buyer"),
            text="/start doc_license",
        )
        with patch.object(
            main, "screen_reply", new_callable=AsyncMock
        ) as render, patch.object(
            main, "show_license_gate", new_callable=AsyncMock
        ) as gate:
            await main.cmd_start(message)
        gate.assert_not_awaited()
        self.assertIn("Лицензионное соглашение", render.call_args.args[1])
        self.assertFalse(main.has_accepted_license(42))
        self.assertEqual(
            render.call_args.kwargs["reply_markup"]
            .inline_keyboard[-1][0]
            .callback_data,
            "legal:gate",
        )

    def test_editions_are_not_retroactively_assigned_to_earlier_acceptance(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO license_accepts(user_id,accepted_at) VALUES(42,'2020-01-01 00:00:00')"
            )
        setup_documents()
        db.init_db()
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM license_accepts WHERE user_id=42"
            ).fetchone()
            self.assertEqual(row["accepted_at"], "2020-01-01 00:00:00")
            self.assertEqual(row["documents_revision"], "")
        main.accept_license(42)
        with db.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT documents_revision FROM license_accepts WHERE user_id=42"
                ).fetchone()[0],
                REVISION,
            )

    async def test_product_display_has_no_diamond_or_catalog_version_and_ends_with_license(
        self,
    ):
        setup_autostars()
        pid = int(db.get_setting("autostars_article_product_id"))
        product = db.get_product(pid)
        db.set_setting("reviews_url", "https://example.org/reviews")
        db.set_setting("reviews_label", " ")
        db.set_setting("license_text", "📜")
        product["article_url"] = "https://telegra.ph/Example-article"
        caption = main.build_product_caption(product)
        self.assertTrue(caption.startswith("<b>AutoStars</b>"))
        self.assertNotIn("💎", caption)
        self.assertIn('>Лицензионное соглашение</a>', caption)
        from services.emoji import PackEmojiMiddleware, AGREEMENT_CACHE_KEY
        db.set_setting(AGREEMENT_CACHE_KEY, "123456")
        caption = main.build_product_caption(product)
        footer = caption.splitlines()[-1]
        self.assertTrue(footer.startswith('<tg-emoji emoji-id="123456">📄</tg-emoji> <a href='))
        self.assertNotIn('<a href="' + document_url("license") + '"><tg-emoji', footer)
        with patch('services.emoji.get_setting', return_value=json.dumps({'📄': '777777'})):
            middleware = PackEmojiMiddleware()
        self.assertEqual(middleware.render_html(footer), footer)
        self.assertNotIn("💬", caption)
        self.assertNotIn("\n\n📜\n", caption)
        self.assertIn('жми</a>\n\n💰 Цена:', caption)
        self.assertIn('</b>\n\n<tg-emoji emoji-id="123456">', caption)
        self.assertIn("Лицензионное соглашение</a>", caption.splitlines()[-1])
        self.assertIn(
            document_url("license").replace("&", "&amp;"), caption.splitlines()[-1]
        )
        target = SimpleNamespace(from_user=SimpleNamespace(id=42))
        with patch.object(main, "safe_edit_or_send", new_callable=AsyncMock) as render:
            await main.render_plugins(target)
        button = render.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertIn("AutoStars", button.text)
        self.assertNotIn(product["version"], button.text)
        self.assertIn("349", button.text)

    def test_readable_pages_fit_messages_and_cross_reference_license(self):
        for key in KEYS:
            pages = document_pages(key)
            self.assertTrue(all(len(page) < 3500 for page in pages))
            self.assertIn("Лицензионное соглашение", "\n".join(pages))
            self.assertIn("14 ноября 2026", pages[0])
        license_text = json.dumps(DOCUMENTS["license"], ensure_ascii=False)
        for product_specific in ("AutoStars", "Tonhub", "Fragment", "24 слова"):
            self.assertNotIn(product_specific, license_text)
        self.assertIn("всему соответствующему ассортименту", license_text)

    def test_one_time_link_update_keeps_operator_edits(self):
        for key in KEYS:
            db.set_setting("document_" + key + "_url", "https://example.org/" + key)
        setup_documents()
        for key in KEYS:
            self.assertEqual(document_url(key), "https://example.org/" + key)
