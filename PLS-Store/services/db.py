"""Database layer with safe migrations for PLS Store."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from config import DB_PATH


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        # --- existing tables (keep compatible) ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                description TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                image_path TEXT,
                article_url TEXT,
                video_url TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                version TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'new'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS license_accepts (
                user_id INTEGER PRIMARY KEY,
                accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # migrations for products / orders
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        for col, sql in [
            ("image_path", "ALTER TABLE products ADD COLUMN image_path TEXT"),
            ("article_url", "ALTER TABLE products ADD COLUMN article_url TEXT"),
            ("video_url", "ALTER TABLE products ADD COLUMN video_url TEXT"),
            ("sort_order", "ALTER TABLE products ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"),
            ("version", "ALTER TABLE products ADD COLUMN version TEXT"),
            ("price_rub", "ALTER TABLE products ADD COLUMN price_rub REAL"),  # numeric price for balance
        ]:
            if col not in cols:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass

        ocols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "status" not in ocols:
            conn.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")

        # --- new tables ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance REAL NOT NULL DEFAULT 0,
                total_spent REAL NOT NULL DEFAULT 0,
                purchases_count INTEGER NOT NULL DEFAULT 0,
                referrer_id INTEGER,
                referral_earnings REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                platega_id TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                payload TEXT,
                redirect_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TEXT
            )
        """)
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_platega ON payments(platega_id)")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                price REAL NOT NULL,
                product_name TEXT,
                status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                admin_id INTEGER,
                payment_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS faq (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                image_path TEXT,
                link TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                discount_type TEXT NOT NULL DEFAULT 'percent',
                value REAL NOT NULL,
                max_uses INTEGER,
                used_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(promo_id, user_id)
            )
        """)

        # seed default design texts if missing
        defaults = {
            "welcome_text": "<b>PLS Store</b>\nПлагины и дополнения для FunPayCardinal\n\nДобро пожаловать, {first_name}!",
            "btn_profile": "👤 Профиль",
            "btn_shop": "🛒 Магазин",
            "btn_language": "🌐 Язык",
            "btn_faq": "❓ FAQ",
            "btn_support": "🎧 Поддержка",
            "btn_topup": "💳 Пополнить баланс",
            "btn_purchases": "🛒 Мои покупки",
            "btn_api": "⚡ API",
            "btn_promo": "🎟 Промокод",
            "btn_referral": "🔗 Рефералка",
            "btn_back": "⬅️ Назад",
            "profile_title": "👤 Профиль",
            "shop_title": "🛒 Магазин PLS Store",
            "faq_title": "❓ FAQ — частые вопросы",
            "topup_title": "💳 Пополнение баланса",
            "referral_percent": "10",
            "min_topup": "100",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings(key, value) VALUES (?, ?)",
                (k, v),
            )

        # seed FAQ if empty
        faq_count = conn.execute("SELECT COUNT(*) AS c FROM faq").fetchone()["c"]
        if faq_count == 0:
            seed_faq = [
                ("🛒 Как купить товар?", "1. Пополните баланс (Профиль → Пополнить).\n2. Откройте Магазин.\n3. Выберите товар → Купить.\n4. Подтвердите списание с баланса.\nТовар появится в «Мои покупки»."),
                ("💳 Как пополнить баланс?", "В Профиле нажмите «Пополнить баланс», укажите сумму и оплатите через Platega (СБП / карты). Средства зачисляются автоматически после подтверждения оплаты."),
                ("💰 Какими способами можно оплатить?", "СБП (QR из банковского приложения), банковские карты и другие методы, доступные в Platega."),
                ("📦 Когда придёт товар?", "Сразу после успешной оплаты с баланса выдача автоматическая. Товар всегда доступен в Профиль → Мои покупки."),
                ("🔄 Возможен ли возврат?", "По вопросам замены и гарантии обращайтесь в Поддержку."),
                ("🎧 Как связаться с поддержкой?", "Кнопка «Поддержка» в главном меню."),
            ]
            for i, (q, a) in enumerate(seed_faq):
                conn.execute(
                    "INSERT INTO faq(question, answer, sort_order) VALUES (?, ?, ?)",
                    (q, a, i),
                )


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO bot_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def ensure_user(user_id: int, username: str | None = None, first_name: str | None = None, referrer_id: int | None = None) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            if username or first_name:
                conn.execute(
                    "UPDATE users SET username=COALESCE(?, username), first_name=COALESCE(?, first_name) WHERE user_id=?",
                    (username, first_name, user_id),
                )
            return dict(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id),
        )
        return dict(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


def get_user(user_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def change_balance(user_id: int, delta: float, reason: str, admin_id: int | None = None, payment_id: int | None = None) -> float:
    """Atomically change balance and write log. Returns new balance."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
            bal = 0.0
        else:
            bal = float(row["balance"])
        new_bal = round(bal + delta, 2)
        if new_bal < 0:
            conn.rollback()
            raise ValueError("Недостаточно средств")
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, user_id))
        conn.execute(
            "INSERT INTO balance_log (user_id, delta, reason, admin_id, payment_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, delta, reason, admin_id, payment_id),
        )
        conn.commit()
        return new_bal


def add_purchase(user_id: int, product_id: int, price: float, product_name: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO purchases (user_id, product_id, price, product_name) VALUES (?, ?, ?, ?)",
            (user_id, product_id, price, product_name),
        )
        conn.execute(
            "UPDATE users SET total_spent = total_spent + ?, purchases_count = purchases_count + 1 WHERE user_id=?",
            (price, user_id),
        )
        return cur.lastrowid


def parse_price_rub(price_str: str, product_row: Any = None) -> float:
    """Extract numeric RUB price from product.price text or price_rub column."""
    if product_row is not None and product_row["price_rub"]:
        try:
            return float(product_row["price_rub"])
        except (TypeError, ValueError):
            pass
    import re
    s = (price_str or "").replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return 0.0


def get_products(active_only: bool = True):
    with db() as conn:
        if active_only:
            return conn.execute(
                "SELECT * FROM products WHERE active=1 ORDER BY sort_order, id"
            ).fetchall()
        return conn.execute("SELECT * FROM products ORDER BY sort_order, id").fetchall()


def get_product(product_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None


def get_faq(active_only: bool = True):
    with db() as conn:
        if active_only:
            return conn.execute(
                "SELECT * FROM faq WHERE active=1 ORDER BY sort_order, id"
            ).fetchall()
        return conn.execute("SELECT * FROM faq ORDER BY sort_order, id").fetchall()
