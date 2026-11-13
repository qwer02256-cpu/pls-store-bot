"""Perpetual entitlements. All mutations use SQLite transactions; no wallet data."""

import secrets
import time
from pathlib import Path
from aiohttp import web
from services.db import db


def migrate(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS license_assets (
      product_id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, instruction TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS licenses (
      id INTEGER PRIMARY KEY, purchase_id INTEGER UNIQUE, user_id INTEGER NOT NULL,
      product_id INTEGER NOT NULL, license_key TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','blocked')),
      funpay_id TEXT, rebind_requested INTEGER NOT NULL DEFAULT 0,
      delivered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS licenses_user ON licenses(user_id);
    CREATE TABLE IF NOT EXISTS license_audit (
      id INTEGER PRIMARY KEY, license_id INTEGER, actor TEXT NOT NULL,
      action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    """)


def audit(conn, lid, actor, action):
    conn.execute(
        "INSERT INTO license_audit(license_id,actor,action) VALUES(?,?,?)",
        (lid, str(actor), action),
    )


def issue(conn, user_id, product_id, purchase_id=None, actor="payment"):
    if purchase_id is not None:
        old = conn.execute(
            "SELECT id FROM licenses WHERE purchase_id=?", (purchase_id,)
        ).fetchone()
        if old:
            return old["id"]
    cur = conn.execute(
        "INSERT INTO licenses(purchase_id,user_id,product_id,license_key) VALUES(?,?,?,?)",
        (purchase_id, user_id, product_id, "PLS-" + secrets.token_urlsafe(32)),
    )
    audit(conn, cur.lastrowid, actor, "issue")
    return cur.lastrowid


def owned(lid, uid):
    with db() as conn:
        r = conn.execute(
            "SELECT * FROM licenses WHERE id=? AND user_id=?", (lid, uid)
        ).fetchone()
        return dict(r) if r else None


def mutate(lid, action, actor, owner=None):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM licenses WHERE id=?", (lid,)).fetchone()
        if not row or (owner is not None and row["user_id"] != owner):
            raise ValueError("Лицензия не найдена")
        if owner is not None and action != "request":
            raise ValueError("Недостаточно прав")
        statements = {
            "block": "status='blocked'",
            "unblock": "status='active'",
            "reset": "funpay_id=NULL,rebind_requested=0",
            "request": "rebind_requested=1",
        }
        if action not in statements:
            raise ValueError("Неизвестное действие")
        if action == "request" and row["rebind_requested"]:
            return False
        conn.execute(
            "UPDATE licenses SET " + statements[action] + " WHERE id=?", (lid,)
        )
        audit(conn, lid, actor, action)
        return True


# Per-process global ceiling avoids unbounded SQLite work/memory on the public route.
# Do not trust X-Forwarded-For: BotHost proxy topology is deployment-specific.
_RATE_KEY = web.AppKey("license_rate", dict)


def add_routes(app):
    app[_RATE_KEY] = {"start": time.monotonic(), "count": 0}
    app.router.add_post("/licenses/verify", verify)
    app.router.add_get("/licenses/health", health)


async def health(request):
    return web.json_response({"ok": True})


async def verify(request):
    now = time.monotonic()
    bucket = request.app.get(_RATE_KEY)
    if bucket is None:
        raise RuntimeError("Use add_routes() to register license endpoints")
    if now - bucket["start"] >= 60:
        bucket.update(start=now, count=0)
    bucket["count"] += 1
    if bucket["count"] > 600:
        return web.json_response(
            {"valid": False}, status=429, headers={"Retry-After": "60"}
        )
    # Strict, bounded payload: never accept arbitrary plugin configuration.
    if request.content_length and request.content_length > 1024:
        return web.json_response({"valid": False}, status=413)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {
            "key",
            "funpay_id",
            "product",
        }:
            raise ValueError()
        key = payload["key"]
        fid = payload["funpay_id"]
        if not isinstance(key, str) or not 20 <= len(key) <= 100:
            raise ValueError()
        if (
            not isinstance(fid, str)
            or not fid.isascii()
            or not fid.isdigit()
            or not 0 < int(fid) < 10**18
        ):
            raise ValueError()
        if payload["product"] != "autostars":
            raise ValueError()
    except (ValueError, TypeError):
        return web.json_response({"valid": False}, status=400)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT l.*,a.file_path FROM licenses l JOIN license_assets a ON a.product_id=l.product_id WHERE license_key=?",
            (key,),
        ).fetchone()
        if (
            not row
            or row["status"] != "active"
            or Path(row["file_path"]).name != "AutoStars.py"
        ):
            return web.json_response(
                {"valid": False}, status=403, headers={"Cache-Control": "no-store"}
            )
        if row["funpay_id"] is None:
            conn.execute(
                "UPDATE licenses SET funpay_id=? WHERE id=?", (str(int(fid)), row["id"])
            )
            audit(conn, row["id"], "plugin", "bind:" + str(int(fid)))
        elif row["funpay_id"] != str(int(fid)):
            return web.json_response(
                {"valid": False}, status=403, headers={"Cache-Control": "no-store"}
            )
    return web.json_response(
        {"valid": True, "perpetual": True}, headers={"Cache-Control": "no-store"}
    )
