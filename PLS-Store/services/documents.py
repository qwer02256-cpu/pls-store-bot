"""Current legal documents, public pages, Telegram copies, and shared links."""

import html
import json
from pathlib import Path
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton
from aiohttp import web
from config import LICENSE_PUBLIC_BASE
from services.db import db, get_setting

REVISION = "2026-11-13"
KEYS = ("privacy", "agreement", "license", "terms")
ROOT = Path(__file__).resolve().parents[1] / "docs/legal"
DOCUMENTS = {key: json.loads((ROOT / f"{key}.json").read_text()) for key in KEYS}
LABELS = {
    "ru": (
        "🔒 Политика конфиденциальности",
        "📜 Пользовательское соглашение",
        "🔑 Лицензионное соглашение",
        "💵 Товары и тарифы",
    ),
    "uk": (
        "🔒 Політика конфіденційності",
        "📜 Угода користувача",
        "🔑 Ліцензійна угода",
        "💵 Товари й тарифи",
    ),
    "en": (
        "🔒 Privacy Policy",
        "📜 User Agreement",
        "🔑 License Agreement",
        "💵 Products and Pricing",
    ),
}


def valid_url(value):
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(c.isspace() for c in value)
        )
    except (ValueError, TypeError):
        return False


def canonical_url(key):
    if key not in DOCUMENTS:
        raise KeyError(key)
    base = LICENSE_PUBLIC_BASE.rstrip("/")
    if valid_url(base):
        return f"{base}/documents/{key}"
    # A document is readable even when no public HTTP origin was configured.
    return f"https://t.me/PLSStore1_bot?start=doc_{key}"


DEFAULT_URLS = {key: canonical_url(key) for key in KEYS}


def document_url(key, license_url=""):
    if key not in DOCUMENTS:
        raise KeyError(key)
    custom = get_setting(f"document_{key}_url", "").strip()
    if key == "license" and not custom:
        custom = license_url.strip() or get_setting("license_url", "").strip()
    return custom if valid_url(custom) else DEFAULT_URLS[key]


def document_rows(lang="ru", license_url=""):
    return [
        [InlineKeyboardButton(text=title, url=document_url(key, license_url))]
        for key, title in zip(KEYS, LABELS.get(lang, LABELS["ru"]))
    ]


def setup_documents():
    """Replace old document links once; retain later operator edits."""
    marker = f"legal_documents_{REVISION}"
    with db() as conn:
        if conn.execute("SELECT 1 FROM bot_settings WHERE key=?", (marker,)).fetchone():
            return
        for key in KEYS:
            setting = f"document_{key}_url"
            old = conn.execute(
                "SELECT value FROM bot_settings WHERE key=?", (setting,)
            ).fetchone()
            if old and old["value"]:
                conn.execute(
                    "INSERT OR IGNORE INTO bot_settings(key,value) VALUES(?,?)",
                    (f"legal_previous_{key}_url", old["value"]),
                )
            for name, value in (
                (setting, DEFAULT_URLS[key]),
                (f"legal_expected_{REVISION}_{key}", DEFAULT_URLS[key]),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO bot_settings(key,value) VALUES(?,?)",
                    (name, value),
                )
        old_license = conn.execute(
            "SELECT value FROM bot_settings WHERE key='license_url'"
        ).fetchone()
        if old_license and old_license["value"]:
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings(key,value) VALUES(?,?)",
                ("legal_previous_license_url", old_license["value"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(key,value) VALUES('license_url',?)",
            (DEFAULT_URLS["license"],),
        )
        conn.execute(
            "INSERT INTO bot_settings(key,value) VALUES(?,?)", (marker, "applied")
        )


def document_pages(key):
    document = DOCUMENTS[key]
    blocks = [
        f'<b>{html.escape(document["title"])}</b>\nРедакция от 13 ноября 2026 года.'
    ]
    for section in document["sections"]:
        blocks.append(f'<b>{html.escape(section["title"])}</b>')
        blocks.extend(html.escape(p) for p in section["paragraphs"])
    blocks.append(
        "<b>Связанные документы</b>\n"
        + "\n".join(
            f'<a href="{html.escape(canonical_url(other), quote=True)}">{html.escape(DOCUMENTS[other]["title"])}</a>'
            for other in KEYS
        )
    )
    pages = [""]
    for block in blocks:
        combined = pages[-1] + ("\n\n" if pages[-1] else "") + block
        if len(combined) > 3300 and pages[-1]:
            pages.append(block)
        else:
            pages[-1] = combined
    return pages


def document_nodes(key):
    document = DOCUMENTS[key]
    nodes = [{"tag": "p", "children": ["Редакция от 13 ноября 2026 года."]}]
    for section in document["sections"]:
        nodes.append({"tag": "h3", "children": [section["title"]]})
        nodes.extend({"tag": "p", "children": [p]} for p in section["paragraphs"])
    nodes.append({"tag": "h3", "children": ["Связанные документы"]})
    nodes.append(
        {
            "tag": "ul",
            "children": [
                {
                    "tag": "li",
                    "children": [
                        {
                            "tag": "a",
                            "attrs": {"href": canonical_url(other)},
                            "children": [DOCUMENTS[other]["title"]],
                        }
                    ],
                }
                for other in KEYS
            ],
        }
    )
    return nodes


def document_html(key):
    document = DOCUMENTS[key]
    links = " ".join(
        f'<a href="{html.escape(canonical_url(other), quote=True)}">{html.escape(DOCUMENTS[other]["title"])}</a>'
        for other in KEYS
    )
    body = "".join(
        "<section><h2>"
        + html.escape(section["title"])
        + "</h2>"
        + "".join("<p>" + html.escape(p) + "</p>" for p in section["paragraphs"])
        + "</section>"
        for section in document["sections"]
    )
    return (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + html.escape(document["title"]) + "</title><style>"
        "body{margin:0;background:#141124;color:#eee;font:17px/1.65 system-ui,sans-serif}"
        "main{max-width:850px;margin:auto;padding:32px 20px}h1,h2{line-height:1.3}h2{font-size:22px;margin-top:32px}"
        "a{color:#c9aaff}nav a,footer a{display:inline-block;margin:8px 18px 8px 0}.brand{color:#b9a0ef}footer{border-top:1px solid #51445e;padding-top:20px}"
        '</style><main><div class="brand">PLS Store</div><nav>'
        + links
        + "</nav><h1>"
        + html.escape(document["title"])
        + "</h1><p>Редакция от 13 ноября 2026 года.</p>"
        + body
        + "<footer><h2>Связанные документы</h2>"
        + links
        + '<p>Обращения: раздел «Поддержка» в <a href="https://t.me/PLSStore1_bot">@PLSStore1_bot</a>.</p></footer></main></html>'
    )


async def document_handler(request):
    key = request.match_info["key"]
    if key not in DOCUMENTS:
        raise web.HTTPNotFound()
    return web.Response(
        text=document_html(key),
        content_type="text/html",
        headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def add_document_routes(app):
    app.router.add_get("/documents/{key}", document_handler)
