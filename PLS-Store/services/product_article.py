"""Approved AutoStars copy and a recoverable one-page Telegraph publisher."""

import asyncio
import html
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from services.db import db, get_setting, set_setting

log = logging.getLogger(__name__)
ARTICLE = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/AUTOSTARS_ARTICLE.json").read_text()
)
SHORT_DESCRIPTION = ARTICLE["short_description"]
SETTING_PREFIX = "autostars_article_"
GIFT_ARTICLE = json.loads((Path(__file__).resolve().parents[1] / "docs/AUTOGIFT_ARTICLE.json").read_text())
GIFT_PREFIX = "autogift_article_"


def is_article_product(product_id):
    return any(str(product_id) == get_setting(prefix + "product_id", "") for prefix in (SETTING_PREFIX, GIFT_PREFIX))


def telegraph_content(article=ARTICLE):
    nodes = [{"tag": "p", "children": [article["intro"]]}]
    for section in article["sections"]:
        nodes.append({"tag": "h3", "children": [section["title"]]})
        if section.get("text"):
            nodes.append({"tag": "p", "children": [section["text"]]})
        if section.get("items"):
            nodes.append(
                {
                    "tag": "ul",
                    "children": [
                        {"tag": "li", "children": [item]} for item in section["items"]
                    ],
                }
            )
    nodes.append(
        {
            "tag": "p",
            "children": [
                {
                    "tag": "a",
                    "attrs": {"href": article["store_url"]},
                    "children": [article["cta"]],
                }
            ],
        }
    )
    return nodes


def article_pages(article=ARTICLE):
    """Telegram HTML pages, under the message limit, without truncating content."""
    blocks = [
        f"<b>{html.escape(article['title'])}</b>\n\n{html.escape(article['intro'])}"
    ]
    for section in article["sections"]:
        block = "<b>" + html.escape(section["title"]) + "</b>"
        if section.get("text"):
            block += "\n" + html.escape(section["text"])
        for item in section.get("items", []):
            block += "\n• " + html.escape(item)
        blocks.append(block)
    blocks.append(
        f'<a href="{html.escape(article["store_url"], quote=True)}">{html.escape(article["cta"])}</a>'
    )
    pages = [""]
    for block in blocks:
        combined = pages[-1] + ("\n\n" if pages[-1] else "") + block
        if len(combined) > 3500 and pages[-1]:
            pages.append(block)
        else:
            pages[-1] = combined
    return pages


def _page_url(value):
    if not isinstance(value, str):
        raise ValueError("Invalid Telegraph page URL")
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.netloc != "telegra.ph"
        or not url.path.strip("/")
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid Telegraph page URL")
    return value


async def _post(client, method, **fields):
    response = await client.post("https://api.telegra.ph/" + method, data=fields)
    response.raise_for_status()
    data = response.json()
    if (
        not isinstance(data, dict)
        or data.get("ok") is not True
        or not isinstance(data.get("result"), dict)
    ):
        raise ValueError("Telegraph did not accept request")
    return data["result"]


async def publish_article(client, article=ARTICLE, prefix=SETTING_PREFIX):
    """Resume using saved account/page; preserve article edits made by the admin."""
    pid = get_setting(prefix + "product_id", "")
    expected = get_setting(prefix + "expected_url", "")
    with db() as conn:
        row = conn.execute(
            "SELECT article_url FROM products WHERE id=?", (pid,)
        ).fetchone()
    if not row or (row["article_url"] or "") != expected:
        return None
    url = get_setting(prefix + "published_url", "")
    if not url:
        token = get_setting(prefix + "access_token", "")
        if not token:
            account = await _post(
                client,
                "createAccount",
                short_name="PLS Store",
                author_name="PLS Store",
                author_url=article["store_url"],
            )
            token = account.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 1024:
                raise ValueError("Invalid Telegraph account")
            # Private SQLite state, never in source code, links, or logs.
            set_setting(prefix + "access_token", token)
        # Recover a page when createPage succeeded but its response was lost.
        listing = await _post(
            client, "getPageList", access_token=token, offset=0, limit=50
        )
        pages = listing.get("pages")
        if not isinstance(pages, list):
            raise ValueError("Invalid Telegraph page list")
        match = next(
            (
                p
                for p in pages
                if isinstance(p, dict) and p.get("title") == article["title"]
            ),
            None,
        )
        if match:
            url = _page_url(match.get("url"))
        else:
            page = await _post(
                client,
                "createPage",
                access_token=token,
                title=article["title"],
                author_name="PLS Store",
                author_url=article["store_url"],
                content=json.dumps(telegraph_content(article), ensure_ascii=False),
                return_content="false",
            )
            url = _page_url(page.get("url"))
        set_setting(prefix + "published_url", url)
    url = _page_url(url)
    # The operator may have edited the card while publication was in flight.
    with db() as conn:
        changed = conn.execute(
            "UPDATE products SET article_url=? WHERE id=? AND coalesce(article_url,'')=?",
            (url, pid, expected),
        ).rowcount
        if changed:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings(key,value) VALUES(?,?)",
                (prefix + "expected_url", url),
            )
    return url if changed else None


async def publication_worker(article=ARTICLE, prefix=SETTING_PREFIX):
    # Never delay bot startup or payments. A failed publication leaves in-bot copy available.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for attempt in range(3):
            try:
                await publish_article(client, article, prefix)
                return
            except Exception as exc:
                # Exception text/request data may contain account credentials.
                log.warning(
                    "Product Telegraph publication unavailable (%s); in-bot article available",
                    type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(60)


def gift_article_url(product_id):
    """Serve bundled copy immediately; a published/admin URL takes precedence."""
    from config import LICENSE_PUBLIC_BASE
    if not product_id or str(product_id) != get_setting(GIFT_PREFIX + 'product_id', ''):
        return ''
    base = urlsplit(LICENSE_PUBLIC_BASE)
    if (base.scheme != 'https' or not base.hostname or base.username or base.password
            or base.path not in ('', '/') or base.query or base.fragment):
        return ''
    return LICENSE_PUBLIC_BASE.rstrip('/') + '/articles/autogift'


def add_article_routes(app):
    from aiohttp import web

    async def gift_article(request):
        parts = ['<h1>' + html.escape(GIFT_ARTICLE['title']) + '</h1>',
                 '<p>' + html.escape(GIFT_ARTICLE['intro']) + '</p>']
        for section in GIFT_ARTICLE['sections']:
            parts.append('<h2>' + html.escape(section['title']) + '</h2>')
            if section.get('text'):
                parts.append('<p>' + html.escape(section['text']) + '</p>')
            if section.get('items'):
                parts.append('<ul>' + ''.join('<li>' + html.escape(item) + '</li>' for item in section['items']) + '</ul>')
        parts.append('<p><a href="https://t.me/PLSStore1_bot">Открыть PLS Store</a></p>')
        page = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>AutoGift · PLS Store</title><style>'
                'body{margin:0;background:#140d24;color:#f6f1ff;font:18px/1.65 system-ui,sans-serif}'
                'main{max-width:820px;margin:auto;padding:32px 22px}h1,h2{line-height:1.25}'
                'h2{margin-top:36px}a{color:#c7a0ff}li{margin:12px 0}'
                '</style></head><body><main>' + ''.join(parts) + '</main></body></html>')
        return web.Response(text=page, content_type='text/html', headers={'X-Content-Type-Options': 'nosniff'})

    app.router.add_get('/articles/autogift', gift_article)
