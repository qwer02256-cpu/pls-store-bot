"""Register the supplied plugin without guessing its selling price."""

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
                "INSERT INTO products(name,price,price_rub,description,version,active) VALUES('AutoStars','Цена не настроена',0,'Автоматическая продажа Telegram Stars для FunPay Cardinal. Бессрочная лицензия на один аккаунт FunPay.','2.1',0)"
            ).lastrowid
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
