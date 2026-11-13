import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import main
from services import db
from services.license_setup import setup_autostars
from services.product_article import (
    ARTICLE,
    SHORT_DESCRIPTION,
    article_pages,
    telegraph_content,
    publish_article,
)


class ProductArticleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.temp.name) / "store.db")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10)
        setup_autostars()
        self.pid = int(db.get_setting("autostars_article_product_id"))
        self.token = "dummy-private-account-token"
        self.url = "https://telegra.ph/AutoStars-PLS-Store-11-13"
        self.methods = []

    def product(self):
        return db.get_product(self.pid)

    def client(self, handler=None):
        async def serve(request):
            from urllib.parse import parse_qs

            self.methods.append(request.url.path)
            fields = {
                key: value[0]
                for key, value in parse_qs(request.content.decode()).items()
            }
            if handler:
                response = handler(request, fields)
                if response is not None:
                    return response
            if request.url.path == "/createAccount":
                result = {"access_token": self.token}
            elif request.url.path == "/getPageList":
                self.assertEqual(fields["access_token"], self.token)
                result = {"total_count": 0, "pages": []}
            elif request.url.path == "/createPage":
                self.assertEqual(fields["access_token"], self.token)
                self.assertEqual(json.loads(fields["content"]), telegraph_content())
                self.assertNotIn(self.token, str(request.url))
                result = {"url": self.url}
            else:
                raise AssertionError("Unexpected API endpoint")
            return httpx.Response(200, json={"ok": True, "result": result})

        return httpx.AsyncClient(transport=httpx.MockTransport(serve))

    def test_copy_installed_once_and_admin_edits_survive(self):
        self.assertEqual(self.product()["description"], SHORT_DESCRIPTION)
        with db.db() as conn:
            conn.execute(
                "UPDATE products SET description='Custom', article_url='https://example.org/manual' WHERE id=?",
                (self.pid,),
            )
        setup_autostars()
        self.assertEqual(self.product()["description"], "Custom")
        self.assertEqual(self.product()["article_url"], "https://example.org/manual")

    async def test_publishes_and_links_page_without_duplicate_on_restart(self):
        async with self.client() as client:
            self.assertEqual(await publish_article(client), self.url)
            self.assertEqual(await publish_article(client), self.url)
        self.assertEqual(
            self.methods, ["/createAccount", "/getPageList", "/createPage"]
        )
        self.assertEqual(self.product()["article_url"], self.url)
        self.assertNotIn(self.token, main.build_product_caption(self.product()))

    async def test_lost_create_response_recovers_existing_page(self):
        def lose(request, fields):
            if request.url.path == "/createPage":
                raise httpx.ReadTimeout("lost response")

        async with self.client(lose) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await publish_article(client)
        self.assertEqual(db.get_setting("autostars_article_access_token"), self.token)
        self.methods.clear()

        def recover(request, fields):
            if request.url.path == "/getPageList":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "total_count": 1,
                            "pages": [{"title": ARTICLE["title"], "url": self.url}],
                        },
                    },
                )

        async with self.client(recover) as client:
            self.assertEqual(await publish_article(client), self.url)
        self.assertEqual(self.methods, ["/getPageList"])

    async def test_api_unavailable_preserves_in_bot_copy(self):
        def unavailable(request, fields):
            return httpx.Response(503)

        async with self.client(unavailable) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await publish_article(client)
        self.assertFalse(self.product()["article_url"])
        self.assertTrue(article_pages())

    async def test_manual_link_change_before_publication_prevents_remote_request(self):
        with db.db() as conn:
            conn.execute(
                "UPDATE products SET article_url=? WHERE id=?",
                ("https://example.org/new", self.pid),
            )
        async with self.client() as client:
            self.assertIsNone(await publish_article(client))
        self.assertEqual(self.methods, [])

    async def test_manual_edit_during_publication_is_preserved(self):
        def change(request, fields):
            if request.url.path == "/createPage":
                with db.db() as conn:
                    conn.execute(
                        "UPDATE products SET article_url=? WHERE id=?",
                        ("https://example.org/new", self.pid),
                    )

        async with self.client(change) as client:
            self.assertIsNone(await publish_article(client))
        self.assertEqual(self.product()["article_url"], "https://example.org/new")

    async def test_bad_result_url_never_enters_card(self):
        def malicious(request, fields):
            if request.url.path == "/createPage":
                return httpx.Response(
                    200,
                    json={"ok": True, "result": {"url": "https://example.org/pretend"}},
                )

        async with self.client(malicious) as client:
            with self.assertRaises(ValueError):
                await publish_article(client)
        self.assertFalse(self.product()["article_url"])

    def test_article_contains_current_terms_and_fits_telegram(self):
        text = "\n".join(article_pages())
        for expected in ("349", "Tonhub", "24", "50", "Бессроч", "12 часов"):
            self.assertIn(expected, text)
        self.assertLess(len(SHORT_DESCRIPTION), 420)
        for page in article_pages():
            self.assertLess(len(page) + 20, 4096)

    async def test_card_and_article_navigation_work_without_published_link(self):
        callback = SimpleNamespace(
            data=f"product:{self.pid}",
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(
            main, "has_accepted_license", return_value=True
        ), patch.object(main, "safe_edit_or_send", new_callable=AsyncMock) as render:
            await main.cb_product(callback)
            rows = render.call_args.kwargs["reply_markup"].inline_keyboard
            self.assertFalse(any(
                (b.callback_data or "").startswith("article:") for row in rows for b in row
            ))
        callback.data = f"article:{self.pid}:0"
        with patch.object(
            main, "has_accepted_license", return_value=True
        ), patch.object(main, "screen_reply", new_callable=AsyncMock) as render:
            await main.cb_product_article(callback)
            self.assertIn("Tonhub", render.call_args.args[1])
            back = render.call_args.kwargs["reply_markup"].inline_keyboard[-1][0]
            self.assertEqual(back.callback_data, f"product:{self.pid}")

    async def test_published_card_uses_inline_article_link_without_extra_button(self):
        async with self.client() as client:
            await publish_article(client)
        callback = SimpleNamespace(
            data=f"product:{self.pid}",
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(
            main, "has_accepted_license", return_value=True
        ), patch.object(main, "safe_edit_or_send", new_callable=AsyncMock) as render:
            await main.cb_product(callback)
            buttons = [
                b
                for row in render.call_args.kwargs["reply_markup"].inline_keyboard
                for b in row
            ]
            self.assertFalse(any(b.url == self.url for b in buttons))
            caption = render.call_args.args[1]
            self.assertIn(f'📄 Статья: <a href="{self.url}">жми</a>', caption)
            self.assertNotIn(f'Статья:\n{self.url}', caption)

    async def test_admin_article_link_rejects_invalid_url_without_saving(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1), text="javascript:bad"
        )
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={"product_id": self.pid, "field": "article_url"}
            ),
            clear=AsyncMock(),
        )
        with patch.object(main, "is_admin", return_value=True), patch.object(
            main, "message_text_with_custom_emoji", return_value=message.text
        ), patch.object(main, "screen_reply", new_callable=AsyncMock) as render:
            await main.adm_edit_value(message, state)
        self.assertFalse(self.product()["article_url"])
        state.clear.assert_not_called()
        self.assertIn("HTTPS", render.call_args.args[1])

    async def test_admin_can_save_own_https_article_link(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1), text="https://telegra.ph/My-page-11-13"
        )
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={"product_id": self.pid, "field": "article_url"}
            ),
            clear=AsyncMock(),
        )
        with patch.object(main, "is_admin", return_value=True), patch.object(
            main, "message_text_with_custom_emoji", return_value=message.text
        ), patch.object(main, "screen_reply", new_callable=AsyncMock):
            await main.adm_edit_value(message, state)
        self.assertEqual(self.product()["article_url"], message.text)
        state.clear.assert_awaited_once()
