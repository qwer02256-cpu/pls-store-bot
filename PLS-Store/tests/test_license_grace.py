import ast
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from services import db, license_leases, license_files
from plugin_lease_support import GUARD_NAMES, defaults, payload


class GraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = "PLS-" + "a" * 43
        self.key_path = self.root / "storage/plugins/AutoStars/license.json"
        self.key_path.parent.mkdir(parents=True)
        self.key_path.write_text(json.dumps({"key": self.key}))
        self.cache = self.key_path.with_name("license_status.json")
        self.wall = 1000000.0
        self.mono = 1000.0
        self.card = SimpleNamespace(account=SimpleNamespace(id=123))
        self.requests = Mock()
        self.online()
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "plugins/AutoStars.py").read_text()
        )
        self.nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in GUARD_NAMES
        ]
        self.restart()

    def restart(self):
        self.env = dict(
            defaults(),
            _PLS_LOCK=threading.Lock(),
            _PLS_CHECKED=0.0,
            _PLS_ACCOUNT=None,
            _PlsPath=lambda p: self.root / p,
            _PLS_ENDPOINT="https://store.example/licenses/verify",
            requests=self.requests,
            _pls_time=SimpleNamespace(
                time=lambda: self.wall, monotonic=lambda: self.mono
            ),
        )
        exec(
            compile(ast.Module(body=self.nodes, type_ignores=[]), "<guard>", "exec"),
            self.env,
        )
        self.guard = self.env["_pls_require_license"]

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def online(self):
        self.requests.post.side_effect = None
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: payload(self.key, "123", int(self.wall))
        )

    def outage(self):
        self.requests.post.side_effect = OSError("server offline")

    def test_approved_client_continues_during_outage_and_restart_without_extending_expiry(
        self,
    ):
        self.guard(self.card)
        original = json.loads(self.cache.read_text())["approval"]["expires_at"]
        self.advance(6 * 3600)
        self.outage()
        self.guard(self.card)
        self.assertEqual(self.env["_PLS_MODE"], "grace")
        self.assertEqual(
            json.loads(self.cache.read_text())["approval"]["expires_at"], original
        )
        self.restart()
        self.guard(self.card)
        self.advance(6 * 3600 - 1)
        self.guard(self.card)
        self.advance(1)
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_first_install_without_online_approval_cannot_use_grace(self):
        self.outage()
        with self.assertRaises(RuntimeError):
            self.guard(self.card)
        self.assertFalse(self.cache.exists())

    def test_explicit_block_clears_disk_approval_even_across_restart(self):
        self.guard(self.card)
        self.advance(61)
        self.requests.post.return_value = SimpleNamespace(
            status_code=403, json=lambda: {"valid": False}
        )
        with self.assertRaises(RuntimeError):
            self.guard(self.card)
        self.assertTrue(json.loads(self.cache.read_text())["denied"])
        self.outage()
        self.restart()
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_valid_false_on_http_200_is_also_denial(self):
        self.guard(self.card)
        self.advance(61)
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: {"valid": False}
        )
        with self.assertRaises(RuntimeError):
            self.guard(self.card)
        self.assertTrue(json.loads(self.cache.read_text())["denied"])

    def test_different_funpay_and_replaced_key_cannot_reuse_approval(self):
        self.guard(self.card)
        self.outage()
        self.card.account.id = 456
        with self.assertRaises(RuntimeError):
            self.guard(self.card)
        self.card.account.id = 123
        self.key_path.write_text(json.dumps({"key": "PLS-" + "b" * 43}))
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_recovery_renews_exactly_twelve_hours_from_new_success(self):
        self.guard(self.card)
        self.advance(6 * 3600)
        self.online()
        self.guard(self.card)
        renewed = int(self.wall) + 43200
        self.assertEqual(
            json.loads(self.cache.read_text())["approval"]["expires_at"], renewed
        )
        self.outage()
        self.advance(7 * 3600)
        self.guard(self.card)
        self.assertEqual(self.env["_PLS_MODE"], "grace")
        self.assertEqual(self.env["_PLS_VALID_UNTIL"], renewed)
        self.advance(5 * 3600)
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_server_failures_and_rate_limit_allow_only_existing_approval(self):
        self.guard(self.card)
        for code in (404, 408, 429, 500, 502, 503):
            self.advance(61)
            self.requests.post.return_value = SimpleNamespace(status_code=code)
            self.guard(self.card)
            self.assertEqual(self.env["_PLS_MODE"], "grace")

    def test_malformed_http_200_during_maintenance_uses_signed_cache(self):
        self.guard(self.card)
        self.advance(61)
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=Mock(side_effect=ValueError("not JSON"))
        )
        self.guard(self.card)
        self.assertEqual(self.env["_PLS_MODE"], "grace")

    def test_tampering_expiry_invalidates_signature(self):
        self.guard(self.card)
        state = json.loads(self.cache.read_text())
        state["approval"]["expires_at"] += 3600
        self.cache.write_text(json.dumps(state))
        self.outage()
        self.restart()
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_invalid_online_signature_is_denial_not_an_outage(self):
        self.guard(self.card)
        self.advance(61)
        bad = payload(self.key, "123", int(self.wall))
        bad["signature"] = "A" * 344
        self.requests.post.return_value = SimpleNamespace(
            status_code=200, json=lambda: bad
        )
        with self.assertRaises(RuntimeError):
            self.guard(self.card)
        self.assertTrue(json.loads(self.cache.read_text())["denied"])

    def test_clock_rollback_is_rejected_during_outage(self):
        self.guard(self.card)
        self.wall -= 600
        self.mono += 61
        self.outage()
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_readonly_cache_cannot_enable_grace(self):
        self.guard(self.card)
        self.advance(61)
        self.outage()
        self.env["_pls_store_status"] = Mock(side_effect=OSError("readonly"))
        with self.assertRaises(RuntimeError):
            self.guard(self.card)

    def test_cached_mode_avoids_a_timeout_for_each_order(self):
        self.guard(self.card)
        self.advance(61)
        self.outage()
        self.guard(self.card)
        count = self.requests.post.call_count
        self.advance(10)
        self.guard(self.card)
        self.guard(self.card)
        self.assertEqual(self.requests.post.call_count, count)
        self.advance(51)
        self.guard(self.card)
        self.assertEqual(self.requests.post.call_count, count + 1)

    def test_payload_and_disk_cache_do_not_contain_fragment_secrets(self):
        self.key_path.write_text(
            json.dumps(
                {
                    "key": self.key,
                    "fragment_jwt": "never-send-jwt",
                    "seed": "never-send-seed",
                }
            )
        )
        self.guard(self.card)
        self.assertEqual(
            set(self.requests.post.call_args.kwargs["json"]),
            {"key", "funpay_id", "product"},
        )
        data = self.cache.read_text()
        self.assertNotIn("never-send", data)
        self.assertNotIn(self.key, data)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "DB_PATH", Path(self.tmp.name) / "db")
        p.start()
        self.addCleanup(p.stop)

    def test_persistent_private_key_survives_process_cache_reset(self):
        first = license_leases.public_key()
        lease = license_leases.sign_lease("PLS-key", "123", now=1000)
        self.assertEqual(lease["approval"]["expires_at"], 44200)
        path = Path(self.tmp.name) / "license_approval_key.pem"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        license_leases._private.cache_clear()
        self.assertEqual(license_leases.public_key(), first)

    def test_delivery_embeds_public_key_without_private_material(self):
        with patch.object(
            license_files, "LICENSE_PUBLIC_BASE", "https://store.example"
        ):
            _, data = license_files.plugin_bytes("plugins/AutoStars.py")
        self.assertNotIn(b"__PLS_LEASE_PUBLIC_KEY__", data)
        self.assertNotIn(b"PRIVATE KEY", data)
        tree = ast.parse(data.decode())
        assignment = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_PLS_PUBLIC_KEY" for t in n.targets
            )
        )
        self.assertEqual(
            ast.literal_eval(assignment.value), license_leases.public_key()
        )

    def test_corrupt_private_key_is_not_silently_replaced(self):
        license_leases.public_key()
        license_leases._private.cache_clear()
        path = Path(self.tmp.name) / "license_approval_key.pem"
        path.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            license_leases.public_key()
        self.assertEqual(path.read_bytes(), b"broken")
