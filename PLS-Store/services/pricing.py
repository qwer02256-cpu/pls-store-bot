"""One price calculation for catalog, balance purchases and Platega shortfalls."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from services.db import db, parse_price_rub
from services.payment_ledger import money


def percent(value):
    try:
        result = Decimal(str(value).strip().replace("%", "").replace(",", "."))
    except InvalidOperation:
        raise ValueError("Введите скидку от 0 до 99 процентов") from None
    if (
        not result.is_finite()
        or not 0 <= result <= 99
        or result != result.quantize(Decimal(".01"))
    ):
        raise ValueError(
            "Введите скидку от 0 до 99, не более двух знаков после запятой"
        )
    return result


def product_price(product):
    product = dict(product)
    base = money(parse_price_rub(product["price"], product))
    sale = percent(product.get("discount_percent", 0))
    return max(
        Decimal(".01"),
        (base * (100 - sale) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
    )


def price_label(product):
    product = dict(product)
    sale = percent(product.get("discount_percent", 0))
    return (
        f"{product_price(product):.2f}₽ (−{sale.normalize():f}%)"
        if sale
        else product["price"]
    )


def set_discount(pid, value, admin_id):
    sale = percent(value)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ValueError("Товар не найден")
        product_price(dict(row, discount_percent=float(sale)))
        conn.execute(
            "UPDATE products SET discount_percent=? WHERE id=?", (float(sale), pid)
        )
        conn.execute(
            "INSERT INTO product_discount_log(product_id,admin_id,old_percent,new_percent) VALUES(?,?,?,?)",
            (pid, admin_id, row["discount_percent"], float(sale)),
        )
