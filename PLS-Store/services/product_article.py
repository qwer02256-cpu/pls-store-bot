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


def is_article_product(product_id):
    return str(product_id) == get_setting(SETTING_PREFIX + "product_id", "")


def telegraph_content():
    nodes = [{"tag": "p", "children": [ARTICLE["intro"]]}]
    for section in ARTICLE["sections"]:
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
                    "attrs": {"href": ARTICLE["store_url"]},
                    "children": [ARTICLE["cta"]],
                }
            ],
        }
    )
    return nodes


def article_pages():
    """Telegram HTML pages, under the message limit, without truncating content."""
    blocks = [
        f"<b>{html.escape(ARTICLE['title'])}</b>\n\n{html.escape(ARTICLE['intro'])}"
    ]
    for section in ARTICLE["sections"]:
        block = "<b>" + html.escape(section["title"]) + "</b>"
        if section.get("text"):
            block += "\n" + html.escape(section["text"])
        for item in section.get("items", []):
            block += "\n• " + html.escape(item)
        blocks.append(block)
    blocks.append(
        f'<a href="{html.escape(ARTICLE["store_url"], quote=True)}">{html.escape(ARTICLE["cta"])}</a>'
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


async def publish_article(client):
    """Resume using saved account/page; preserve article edits made by the admin."""
    pid = get_setting(SETTING_PREFIX + "product_id", "")
    expected = get_setting(SETTING_PREFIX + "expected_url", "")
    with db() as conn:
        row = conn.execute(
            "SELECT article_url FROM products WHERE id=?", (pid,)
        ).fetchone()
    if not row or (row["article_url"] or "") != expected:
        return None
    url = get_setting(SETTING_PREFIX + "published_url", "")
    if not url:
        token = get_setting(SETTING_PREFIX + "access_token", "")
        if not token:
            account = await _post(
                client,
                "createAccount",
                short_name="PLS Store",
                author_name="PLS Store",
                author_url=ARTICLE["store_url"],
            )
            token = account.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 1024:
                raise ValueError("Invalid Telegraph account")
            # Private SQLite state, never in source code, links, or logs.
            set_setting(SETTING_PREFIX + "access_token", token)
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
                if isinstance(p, dict) and p.get("title") == ARTICLE["title"]
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
                title=ARTICLE["title"],
                author_name="PLS Store",
                author_url=ARTICLE["store_url"],
                content=json.dumps(telegraph_content(), ensure_ascii=False),
                return_content="false",
            )
            url = _page_url(page.get("url"))
        set_setting(SETTING_PREFIX + "published_url", url)
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
                (SETTING_PREFIX + "expected_url", url),
            )
    return url if changed else None


async def publication_worker():
    # Never delay bot startup or payments. A failed publication leaves in-bot copy available.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for attempt in range(3):
            try:
                await publish_article(client)
                return
            except Exception as exc:
                # Exception text/request data may contain account credentials.
                log.warning(
                    "AutoStars Telegraph publication unavailable (%s); in-bot article available",
                    type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(60)
