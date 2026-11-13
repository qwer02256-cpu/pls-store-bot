"""Register the supplied plugin and apply approved catalog releases once."""

from pathlib import Path
from hashlib import sha256
from services.db import db
from services.licenses import audit

ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS_BUNDLED_GUIDES = {
    "d8a5b97e84a89def77468e190fb5b401920bdd2837840085964fca61ad401a0e",
    "8c00154eb19e129e4d625bcfb29a577bac5fb1ab217758cfc3e7d60b3b2fd9af",
    "1830ac6a456cea506b538181607d67ea587d27162be070c45a0870a1910710d1",
    "34da490eeb5cc988619272b9966d3c16c40c6bdcd96133ee2e46e4400308f02d",
    "dc1e7de752676901775d0bb7d9b14b42df0e85a346da80355c4ec85cd4e53260",
    "71329ee4081069c82deb88c1b9cc415ffdf83a05594a1689e4c7b33f4d9c1e36",
    "a8b49a0c8c5640cbb6eaed0eeb99570b490b57d8aea3a7ceb00084918857c474",
}


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
                "INSERT INTO products(name,price,price_rub,description,version,active) VALUES('AutoStars','349₽ / Навсегда',349,'Автоматическая продажа Telegram Stars для FunPay Cardinal. Бессрочная лицензия на один аккаунт FunPay.','2.5',1)"
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

        # Apply the built-in release once; later catalog version edits must persist.
        if not conn.execute(
            "SELECT 1 FROM bot_settings WHERE key='autostars_bundle_version_2_5'"
        ).fetchone():
            conn.execute(
                "UPDATE products SET version='2.5' WHERE id=? AND (version IS NULL OR version IN ('','2.1','2.2','2.3','2.4','2.5')) AND EXISTS (SELECT 1 FROM license_assets WHERE product_id=? AND file_path='plugins/AutoStars.py')",
                (pid, pid),
            )
            conn.execute(
                "INSERT INTO bot_settings(key,value) VALUES('autostars_bundle_version_2_5','applied')"
            )

        existing = conn.execute(
            "SELECT instruction FROM license_assets WHERE product_id=?",
            (pid,),
        ).fetchone()
        if (
            existing
            and sha256(existing["instruction"].encode()).hexdigest()
            in _PREVIOUS_BUNDLED_GUIDES
        ):
            conn.execute(
                "UPDATE license_assets SET instruction=? WHERE product_id=?",
                ((ROOT / "docs/AUTOSTARS_INSTALL.txt").read_text(), pid),
            )
            audit(conn, None, "setup", f"asset:{pid}:functional_guide")
