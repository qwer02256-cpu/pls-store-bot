"""Manual services sold through the normal balance and Platega checkout."""
from services.db import db

FRAGMENT_DESCRIPTION = (
    "Верификация Fragment выполняется в течение 1–24 часов с входом в ваш "
    "Telegram-аккаунт. После покупки необходимо написать @uservkru "
    "для согласования выполнения услуги."
)
FRAGMENT_FULFILLMENT = (
    "Для выполнения услуги «Верификация Fragment» необходимо написать @uservkru. "
    "Сообщите номер покупки. Верификация выполняется в течение 1–24 часов "
    "с входом в ваш Telegram-аккаунт."
)


def setup_services():
    """Insert once; preserve subsequent administrator edits and visibility."""
    with db() as conn:
        key = 'fragment_verification_service_product_id'
        if conn.execute("SELECT 1 FROM bot_settings WHERE key=?", (key,)).fetchone():
            return
        cur = conn.execute(
            "INSERT INTO products(name,price,price_rub,description,category,fulfillment_message) "
            "VALUES(?,?,?,?,?,?)",
            ('Верификация Fragment', '349 ₽', 349, FRAGMENT_DESCRIPTION, 'services', FRAGMENT_FULFILLMENT),
        )
        conn.execute("INSERT INTO bot_settings(key,value) VALUES(?,?)", (key, str(cur.lastrowid)))
