"""Register the supplied plugin and apply approved catalog releases once."""

from pathlib import Path
from services.db import db
from services.licenses import audit

ROOT = Path(__file__).resolve().parents[1]


def setup_autostars():
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM products WHERE lower(replace(name,' ',''))='autostars'"
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(
                "Несколько товаров AutoStars: требуется однозначная привязка файла"
            )
        if rows:
            pid = rows[0]["id"]
        else:
            pid = conn.execute(
                "INSERT INTO products(name,price,price_rub,description,version,active) VALUES('AutoStars','349₽ / Навсегда',349,'Автоматическая продажа Telegram Stars для FunPay Cardinal. Бессрочная лицензия на один аккаунт FunPay.','2.1',1)"
            ).lastrowid
        if not conn.execute(
            "SELECT 1 FROM bot_settings WHERE key='autostars_price_349_v1'"
        ).fetchone():
            conn.execute(
                "UPDATE products SET price='349₽ / Навсегда',price_rub=349,active=1 WHERE id=?",
                (pid,),
            )
            conn.execute(
                "INSERT INTO bot_settings(key,value) VALUES('autostars_price_349_v1','applied')"
            )
            audit(conn, None, "setup", f"catalog:{pid}:price349")
        if not conn.execute(
            "SELECT 1 FROM license_assets WHERE product_id=?", (pid,)
        ).fetchone():
            conn.execute(
                "INSERT INTO license_assets(product_id,file_path,instruction) VALUES(?,?,?)",
                (
                    pid,
                    "plugins/AutoStars.py",
                    (ROOT / "docs/AUTOSTARS_INSTALL.txt").read_text(),
                ),
            )
            audit(conn, None, "setup", f"asset:{pid}:autostars")
