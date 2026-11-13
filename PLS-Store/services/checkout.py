"""Balance-first checkout. Quotes never reserve or debit an unpaid balance."""

from decimal import Decimal
from services.db import db
from services.payment_ledger import money
from services.licenses import issue
from services.license_files import ready
from services.pricing import product_price


def balance_money(value):
    amount = Decimal(str(value))
    if (
        not amount.is_finite()
        or amount < 0
        or amount != amount.quantize(Decimal("0.01"))
    ):
        raise ValueError("Invalid balance")
    return amount


def prepare_checkout(uid, pid, token):
    if (
        not isinstance(token, str)
        or len(token) != 24
        or any(c not in "0123456789abcdef" for c in token)
    ):
        raise ValueError("Invalid checkout")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM checkouts WHERE id=?", (token,)).fetchone()
        if old:
            if old["user_id"] != uid or old["product_id"] != pid:
                raise ValueError("Checkout owner mismatch")
            return dict(old)
        product = conn.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (pid,)
        ).fetchone()
        if not product or not ready(conn, pid):
            raise ValueError("Товар ещё не готов к автоматической выдаче")
        price = product_price(product)
        conn.execute(
            "INSERT INTO checkouts(id,user_id,product_id,product_name,price) VALUES(?,?,?,?,?)",
            (token, uid, pid, product["name"], float(price)),
        )
        return dict(
            conn.execute("SELECT * FROM checkouts WHERE id=?", (token,)).fetchone()
        )


def record_purchase(conn, user, pid, name, price, balance_debit, payment_id=None):
    """Caller owns BEGIN IMMEDIATE; purchase, debit and license commit together."""
    price = money(price)
    debit = balance_money(balance_debit)
    balance = balance_money(user["balance"]) - debit
    if balance < 0:
        raise ValueError("Недостаточно средств")
    cur = conn.execute(
        "INSERT INTO purchases(user_id,product_id,price,product_name) VALUES(?,?,?,?)",
        (user["user_id"], pid, float(price), name),
    )
    issue(conn, user["user_id"], pid, cur.lastrowid)
    conn.execute(
        "UPDATE users SET balance=?,total_spent=total_spent+?,purchases_count=purchases_count+1 WHERE user_id=?",
        (float(balance), float(price), user["user_id"]),
    )
    if debit:
        conn.execute(
            "INSERT INTO balance_log(user_id,delta,reason,payment_id) VALUES(?,?,?,?)",
            (user["user_id"], -float(debit), f"Purchase #{cur.lastrowid}", payment_id),
        )
    conn.execute(
        "INSERT INTO orders(user_id,username,product_id,status) VALUES(?,?,?,'completed')",
        (user["user_id"], user["username"], pid),
    )
    return dict(
        purpose="purchase",
        user_id=user["user_id"],
        amount=float(price),
        balance=float(balance),
        purchase_id=cur.lastrowid,
        product_name=name,
        referrer_id=None,
        referral_bonus=0,
        balance_debit=float(debit),
        external_amount=float(price - debit),
    )


def balance_checkout(uid, token):
    """Complete once if fully funded, otherwise return a fresh nonbinding quote."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        checkout = conn.execute(
            "SELECT * FROM checkouts WHERE id=? AND user_id=?", (token, uid)
        ).fetchone()
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not checkout or not user:
            raise ValueError("Покупка не найдена")
        if checkout["purchase_id"] is not None:
            return dict(completed=True, result=None, checkout=dict(checkout))
        if not ready(conn, checkout["product_id"]):
            raise ValueError("Товар ещё не готов к автоматической выдаче")
        balance = balance_money(user["balance"])
        price = money(checkout["price"])
        if balance >= price:
            result = record_purchase(
                conn,
                user,
                checkout["product_id"],
                checkout["product_name"],
                price,
                price,
            )
            conn.execute(
                "UPDATE checkouts SET purchase_id=? WHERE id=?",
                (result["purchase_id"], token),
            )
            return dict(completed=True, result=result, checkout=dict(checkout))
        return dict(
            completed=False,
            result=None,
            checkout=dict(checkout),
            balance_part=float(balance),
            remaining=float(price - balance),
        )
