"""Buyer-facing balance breakdown in the store's supported languages."""

from html import escape


def quote_text(lang, name, price, balance, due, tx_id=None):
    texts = {
        "ru": (
            "Оплата товара",
            "Товар",
            "Цена",
            "С баланса",
            "Доплатить",
            "Выберите способ доплаты.",
            "Деньги с баланса спишутся только после подтверждения доплаты. Если не оплатите, они останутся на балансе.",
        ),
        "uk": (
            "Оплата товару",
            "Товар",
            "Ціна",
            "З балансу",
            "Доплатити",
            "Виберіть спосіб доплати.",
            "Кошти з балансу спишуться лише після підтвердження доплати. Якщо не оплатите, вони залишаться на балансі.",
        ),
        "en": (
            "Product payment",
            "Product",
            "Price",
            "From balance",
            "Amount due",
            "Choose a payment method.",
            "Your balance is charged only after the additional payment is confirmed. If you do not pay, your balance stays unchanged.",
        ),
    }
    title, product, cost, part, remaining, prompt, note = texts.get(lang, texts["ru"])
    return (
        f"🧾 <b>{title}</b>\n\n{product}: <b>{escape(name)}</b>\n{cost}: <b>{price:.2f}₽</b>\n"
        f"{part}: <b>{balance:.2f}₽</b>\n{remaining}: <b>{due:.2f}₽</b>\n\n"
        + (f"ID: <code>{escape(str(tx_id))}</code>\n\n" if tx_id else prompt + "\n\n")
        + note
    )


def receipt_text(lang, balance_part, external, balance):
    labels = {
        "ru": ("Списано с баланса", "Доплата через Platega", "Остаток на балансе"),
        "uk": ("Списано з балансу", "Доплата через Platega", "Залишок на балансі"),
        "en": ("Charged from balance", "Paid via Platega", "Balance remaining"),
    }
    charged, paid, left = labels.get(lang, labels["ru"])
    return f"{charged}: <b>{balance_part:.2f}₽</b>\n{paid}: <b>{external:.2f}₽</b>\n{left}: <b>{balance:.2f}₽</b>"


def credit_text(lang, amount, balance, reason, remaining):
    if reason == "already_completed":
        return {
            "ru": f"Эта покупка уже оплачена. Повторная оплата <b>{amount:.2f}₽</b> зачислена на ваш баланс.\nБаланс: <b>{balance:.2f}₽</b>.\nФайл и ключ доступны в разделе «Лицензии».",
            "uk": f"Цю покупку вже оплачено. Повторну оплату <b>{amount:.2f}₽</b> зараховано на ваш баланс.\nБаланс: <b>{balance:.2f}₽</b>.\nФайл і ключ доступні в розділі «Ліцензії».",
            "en": f"This checkout is already paid. Your additional payment of <b>{amount:.2f}₽</b> was credited to your balance.\nBalance: <b>{balance:.2f}₽</b>.\nFind your file and key in Licenses.",
        }.get(lang)
    return {
        "ru": f"✅ Доплата <b>{amount:.2f}₽</b> получена и сохранена на вашем балансе.\nБаланс: <b>{balance:.2f}₽</b>.\n\nПосле создания счёта баланс изменился. Для завершения покупки не хватает <b>{remaining:.2f}₽</b>. Нажмите «Продолжить покупку».",
        "uk": f"✅ Доплату <b>{amount:.2f}₽</b> отримано та збережено на вашому балансі.\nБаланс: <b>{balance:.2f}₽</b>.\n\nПісля створення рахунку баланс змінився. Для завершення покупки бракує <b>{remaining:.2f}₽</b>. Натисніть «Продовжити покупку».",
        "en": f"✅ Your payment of <b>{amount:.2f}₽</b> was saved to your balance.\nBalance: <b>{balance:.2f}₽</b>.\n\nYour balance changed after the invoice was created. You still need <b>{remaining:.2f}₽</b> to complete this purchase. Choose Continue purchase.",
    }.get(lang)
