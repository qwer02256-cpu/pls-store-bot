import ast
import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from services import db, license_files
from services.licenses import issue, owned, mutate, add_routes, _RATE_KEY
from services.payment_ledger import settle_payment
from services.license_setup import setup_autostars
from handlers import licenses as handlers
import main


class LicenseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(db, "DB_PATH", self.root / "db.sqlite")
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        db.ensure_user(10)
        with db.db() as conn:
            self.pid = conn.execute(
                "INSERT INTO products(name,price,description) VALUES('Plugin','100','d')"
            ).lastrowid
            conn.execute(
                "INSERT INTO license_assets VALUES(?,?,?)",
                (self.pid, "plugins/AutoStars.py", "Guide"),
            )
            self.lid = issue(conn, 10, self.pid)
        self.key = owned(self.lid, 10)["license_key"]

    async def client(self):
        app = web.Application(client_max_size=4096)
        add_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def payload(self, fid="123"):
        return dict(key=self.key, funpay_id=fid, product="autostars")

    async def test_first_request_binds_then_other_account_rejected(self):
        c = await self.client()
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload())).status, 200
        )
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload())).status, 200
        )
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload("456"))).status, 403
        )
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "123")
        with db.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM license_audit WHERE action LIKE 'bind:%'"
                ).fetchone()[0],
                1,
            )

    async def test_simultaneous_first_binding_only_one_account_wins(self):
        c = await self.client()
        replies = await asyncio.gather(
            *(
                c.post("/licenses/verify", json=self.payload(fid))
                for fid in ("111", "222")
            )
        )
        self.assertEqual(sorted(r.status for r in replies), [200, 403])

    async def test_block_unblock_reset(self):
        c = await self.client()
        await c.post("/licenses/verify", json=self.payload())
        mutate(self.lid, "block", 1)
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload())).status, 403
        )
        mutate(self.lid, "unblock", 1)
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload())).status, 200
        )
        mutate(self.lid, "reset", 1)
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload("987"))).status, 200
        )
        self.assertEqual(owned(self.lid, 10)["funpay_id"], "987")

    async def test_bad_payloads_and_secret_fields_never_accepted(self):
        c = await self.client()
        payloads = [
            [],
            {},
            self.payload("0"),
            self.payload("-1"),
            self.payload("１２３"),
            self.payload("1" * 40),
            dict(self.payload(), seed="secret"),
            dict(self.payload(), product="other"),
            dict(self.payload(), key=[]),
            dict(self.payload(), funpay_id=123),
        ]
        for payload in payloads:
            self.assertEqual(
                (await c.post("/licenses/verify", json=payload)).status, 400
            )
        self.assertEqual((await c.post("/licenses/verify", data="{bad")).status, 400)
        self.assertEqual(
            (
                await c.post(
                    "/licenses/verify", json=dict(self.payload(), key="x" * 3000)
                )
            ).status,
            413,
        )
        self.assertIsNone(owned(self.lid, 10)["funpay_id"])

    async def test_unknown_key_and_wrong_product_asset(self):
        c = await self.client()
        self.assertEqual(
            (
                await c.post(
                    "/licenses/verify", json=dict(self.payload(), key="PLS-" + "x" * 43)
                )
            ).status,
            403,
        )
        with db.db() as conn:
            conn.execute("UPDATE license_assets SET file_path='other.py'")
        self.assertEqual(
            (await c.post("/licenses/verify", json=self.payload())).status, 403
        )

    def test_ownership_and_rebind_request_idempotency(self):
        self.assertIsNone(owned(self.lid, 99))
        with self.assertRaises(ValueError):
            mutate(self.lid, "request", 99, owner=99)
        with self.assertRaises(ValueError):
            mutate(self.lid, "reset", 10, owner=10)
        self.assertTrue(mutate(self.lid, "request", 10, owner=10))
        self.assertFalse(mutate(self.lid, "request", 10, owner=10))
        self.assertEqual(owned(self.lid, 10)["rebind_requested"], 1)

    def invoice(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO payments(user_id,amount,platega_id,purpose,product_id,product_name) VALUES(10,100,'tx','purchase',?,'Plugin')",
                (self.pid,),
            )

    def test_concurrent_confirmation_issues_exactly_one_license(self):
        self.invoice()
        data = dict(id="tx", status="CONFIRMED", amount=100, currency="RUB")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: settle_payment("tx", data), range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        with db.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM licenses WHERE purchase_id IS NOT NULL"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM purchases").fetchone()[0], 1
            )

    def test_license_failure_rolls_back_entire_payment(self):
        self.invoice()
        with db.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_license BEFORE INSERT ON licenses BEGIN SELECT RAISE(ABORT,'failure'); END"
            )
        with self.assertRaises(Exception):
            settle_payment("tx", dict(status="CONFIRMED", amount=100))
        with db.db() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM purchases").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT status FROM payments").fetchone()[0], "PENDING"
            )
        self.assertEqual(db.get_user(10)["total_spent"], 0)

    async def test_delivery_same_key_and_failures_remain_pending(self):
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        with patch.object(
            license_files, "LICENSE_PUBLIC_BASE", "https://store.example"
        ):
            self.assertTrue(await handlers.deliver(bot, self.lid, 10))
            self.assertTrue(await handlers.deliver(bot, self.lid, 10))
        self.assertEqual(bot.send_document.await_count, 4)
        docs = bot.send_document.await_args_list
        self.assertIn(self.key, bot.send_message.await_args_list[0].args[1])
        self.assertEqual(
            [c.args[1].filename for c in docs],
            ["AutoStars.py", "INSTALL.txt", "AutoStars.py", "INSTALL.txt"],
        )
        self.assertIn(b"https://store.example/licenses/verify", docs[0].args[1].data)
        self.assertNotIn(b"__PLS_LICENSE_ENDPOINT__", docs[0].args[1].data)
        with db.db() as conn:
            conn.execute("UPDATE licenses SET delivered=0")
        bot.send_document.side_effect = RuntimeError("Telegram offline")
        with patch.object(
            license_files, "LICENSE_PUBLIC_BASE", "https://store.example"
        ):
            with self.assertRaises(RuntimeError):
                await handlers.deliver(bot, self.lid, 10)
        self.assertEqual(owned(self.lid, 10)["delivered"], 0)
        self.assertEqual(owned(self.lid, 10)["license_key"], self.key)

    async def test_blocked_or_foreign_license_cannot_download(self):
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        self.assertFalse(await handlers.deliver(bot, self.lid, 99))
        mutate(self.lid, "block", 1)
        self.assertFalse(await handlers.deliver(bot, self.lid, 10))
        bot.send_document.assert_not_awaited()

    async def test_buyer_cannot_use_admin_commands(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=99),
            text="/license_issue 99 1",
            answer=AsyncMock(),
        )
        with patch.object(handlers, "ADMIN_ID", 1):
            await handlers.manual_issue(message, Mock())
            await handlers.admin_action(message)
            await handlers.search(message)
            await handlers.logs(message)
        message.answer.assert_not_awaited()

    async def test_rate_limit_is_bounded_and_health_available(self):
        c = await self.client()
        c.server.app[_RATE_KEY]["count"] = 600
        response = await c.post("/licenses/verify", json=self.payload())
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertEqual((await c.get("/licenses/health")).status, 200)

    async def test_delivery_queue_does_not_starve_later_buyers(self):
        with db.db() as conn:
            lid2 = issue(conn, 10, self.pid)
        bot = Mock()
        with patch.object(
            handlers, "deliver", AsyncMock(side_effect=RuntimeError("offline"))
        ) as delivery:
            cursor = await handlers.reconcile_deliveries(bot, limit=1)
            self.assertEqual(cursor, self.lid)
            self.assertEqual(
                await handlers.reconcile_deliveries(bot, cursor, limit=1), lid2
            )
            self.assertEqual(delivery.await_count, 2)

    async def test_admin_can_upload_guide_and_file_with_audit(self):
        import io

        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"pid": self.pid, "kind": "guide"}),
            clear=AsyncMock(),
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            text="Detailed custom guide",
            document=None,
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
        )
        with patch.object(handlers, "ADMIN_ID", 1):
            await handlers.upload(message, state, Mock())
            old_file = self.root / "old_plugin.py"
            old_file.write_text("VERSION='1.0'")
            with db.db() as conn:
                conn.execute(
                    "UPDATE license_assets SET file_path=? WHERE product_id=?",
                    (str(old_file), self.pid),
                )
            state.get_data.return_value = {"pid": self.pid, "kind": "file"}
            message.document = SimpleNamespace(file_name="plugin.py", file_size=12)

            async def download(doc, destination):
                Path(destination).write_text("# new plugin\nVERSION = '3.0'")

            bot = SimpleNamespace(download=AsyncMock(side_effect=download))
            with patch.object(handlers, "DB_PATH", self.root / "db.sqlite"):
                await handlers.upload(message, state, bot)
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM license_assets WHERE product_id=?", (self.pid,)
            ).fetchone()
            self.assertEqual(row["instruction"], "Detailed custom guide")
            self.assertEqual(
                Path(row["file_path"]).read_text(), "# new plugin\nVERSION = '3.0'"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM license_audit WHERE action LIKE 'asset:%'"
                ).fetchone()[0],
                2,
            )

    def test_registration_migration_preserve_existing_settings(self):
        setup_autostars()
        setup_autostars()
        db.init_db()
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE name='AutoStars'"
            ).fetchone()
            self.assertEqual(row["active"], 1)
            self.assertEqual(row["price_rub"], 349)
            self.assertEqual(row["price"], "349₽ / Навсегда")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM products WHERE name='AutoStars'"
                ).fetchone()[0],
                1,
            )
            conn.execute(
                "UPDATE products SET price='399₽',price_rub=399,active=0 WHERE id=?",
                (row["id"],),
            )
            conn.execute(
                "UPDATE license_assets SET instruction='custom' WHERE product_id=?",
                (row["id"],),
            )
        setup_autostars()
        with db.db() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=?", (row["id"],)
            ).fetchone()
            self.assertEqual(
                (product["price"], product["price_rub"], product["active"]),
                ("399₽", 399, 0),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT instruction FROM license_assets WHERE product_id=?",
                    (row["id"],),
                ).fetchone()[0],
                "custom",
            )

    def test_existing_autostars_gets_approved_price_once(self):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO products(name,price,price_rub,description,active) VALUES('AutoStars','Цена не настроена',0,'d',0)"
            )
        setup_autostars()
        with db.db() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE name='AutoStars'"
            ).fetchone()
            self.assertEqual((row["price_rub"], row["active"]), (349, 1))

    async def test_unprepared_product_never_creates_invoice(self):
        message = SimpleNamespace(answer=AsyncMock())
        provider = SimpleNamespace(create_payment=AsyncMock())
        with db.db() as conn:
            conn.execute("DELETE FROM license_assets")
        with patch.object(main, "platega", provider):
            await main.create_payment_invoice(
                message,
                SimpleNamespace(id=10, username=None, first_name="X"),
                100,
                Mock(),
                product={"id": self.pid},
            )
        provider.create_payment.assert_not_awaited()

    def test_invalid_origin_prevents_autostars_sale(self):
        for origin in (
            "",
            "http://store.example",
            "https://x@y.test",
            "https://x.test/path",
            "https://x.test?q=x",
        ):
            with patch.object(
                license_files, "LICENSE_PUBLIC_BASE", origin
            ), db.db() as conn:
                self.assertFalse(license_files.ready(conn, self.pid))

    async def test_actual_server_routes_without_platega_base(self):
        with patch.object(main, "PLATEGA_CALLBACK_PORT", 0), patch.object(
            main, "PLATEGA_CALLBACK_BASE", ""
        ):
            runner = await main.start_callback_server(Mock())
        self.addAsyncCleanup(runner.cleanup)
        routes = {(r.method, r.resource.canonical) for r in runner.app.router.routes()}
        self.assertIn(("POST", "/licenses/verify"), routes)
        self.assertIn(("POST", "/platega/callback"), routes)


from plugin_lease_support import (
    GUARD_NAMES,
    defaults as guard_defaults,
    payload as lease_payload,
)


class PluginGuardTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "plugins/AutoStars.py").read_text()
        )
        nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in GUARD_NAMES
        ]
        import threading

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "storage/plugins/AutoStars/license.json"
        self.path.parent.mkdir(parents=True)
        self.key = "PLS-" + "a" * 43
        self.path.write_text(
            json.dumps(
                {"key": self.key, "seed": "never send", "fragment_jwt": "never send"}
            )
        )
        self.requests = Mock()
        self.requests.post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: lease_payload(self.key, "123", int(self.clock())),
        )
        self.clock = Mock(return_value=1000)
        self.env = dict(
            json=json,
            _PLS_LOCK=threading.Lock(),
            _PLS_CHECKED=0.0,
            _PLS_ACCOUNT=None,
            _PlsPath=lambda p: self.root / p,
            _PLS_ENDPOINT="https://store.example/licenses/verify",
            requests=self.requests,
            _pls_time=SimpleNamespace(monotonic=self.clock, time=self.clock),
        )
        self.env.update(guard_defaults())
        exec(
            compile(ast.Module(body=nodes, type_ignores=[]), "<guard>", "exec"),
            self.env,
        )
        self.guard = self.env["_pls_require_license"]
        self.cardinal = SimpleNamespace(account=SimpleNamespace(id=123))
        self.tree = tree

    def test_only_allowlisted_payload_and_bounded_cache(self):
        self.guard(self.cardinal)
        self.guard(self.cardinal)
        self.assertEqual(self.requests.post.call_count, 1)
        kwargs = self.requests.post.call_args.kwargs
        self.assertEqual(set(kwargs["json"]), {"key", "funpay_id", "product"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (5, 10))
        self.clock.return_value = 1061
        self.guard(self.cardinal)
        self.assertEqual(self.requests.post.call_count, 2)

    def test_failure_revokes_cache_and_account_changes_force_recheck(self):
        self.guard(self.cardinal)
        self.cardinal.account.id = 456
        self.requests.post.return_value = SimpleNamespace(
            status_code=403, json=lambda: {"valid": False}
        )
        with self.assertRaises(RuntimeError):
            self.guard(self.cardinal)
        self.assertIsNone(self.env["_PLS_ACCOUNT"])
        self.assertEqual(self.env["_PLS_CHECKED"], 0)

    def test_network_error_fails_closed_and_missing_account(self):
        self.requests.post.side_effect = OSError("network")
        with self.assertRaises(RuntimeError):
            self.guard(self.cardinal)
        self.cardinal.account.id = None
        with self.assertRaises(RuntimeError):
            self.guard(self.cardinal)

    def test_all_sale_entrypoints_guarded(self):
        for name in (
            "new_order_handler",
            "new_message_handler",
            "_do_confirm_send",
            "_order_stars",
        ):
            node = next(
                n
                for n in self.tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name
            )
            self.assertIsInstance(node.body[0], ast.Expr)
            self.assertEqual(node.body[0].value.func.id, "_pls_require_license")
