"""Publish current legal editions without blocking the store or losing recovery state."""

import asyncio
import logging
import json

import httpx
from services.db import db, get_setting, set_setting
from services.documents import KEYS, DOCUMENTS, REVISION, document_nodes
from services.product_article import _post, _page_url

log = logging.getLogger(__name__)


async def publish_documents(client):
    token = get_setting("legal_telegraph_access_token", "")
    if not token:
        result = await _post(
            client,
            "createAccount",
            short_name="PLS Store Docs",
            author_name="PLS Store",
            author_url="https://t.me/PLSStore1_bot",
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 1024:
            raise ValueError("Invalid Telegraph account")
        set_setting("legal_telegraph_access_token", token)
    for key in KEYS:
        expected_key = f"legal_expected_{REVISION}_{key}"
        expected = get_setting(expected_key, "")
        current = get_setting(f"document_{key}_url", "")
        if not expected or current != expected:
            continue
        published_key = f"legal_published_{REVISION}_{key}"
        url = get_setting(published_key, "")
        if not url:
            listing = await _post(
                client, "getPageList", access_token=token, offset=0, limit=50
            )
            pages = listing.get("pages")
            if not isinstance(pages, list):
                raise ValueError("Invalid Telegraph page list")
            title = DOCUMENTS[key]["title"] + " · " + REVISION
            match = next(
                (
                    page
                    for page in pages
                    if isinstance(page, dict) and page.get("title") == title
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
                    title=title,
                    author_name="PLS Store",
                    author_url="https://t.me/PLSStore1_bot",
                    content=json.dumps(document_nodes(key), ensure_ascii=False),
                    return_content="false",
                )
                url = _page_url(page.get("url"))
            set_setting(published_key, url)
        url = _page_url(url)
        with db() as conn:
            changed = conn.execute(
                "UPDATE bot_settings SET value=? WHERE key=? AND value=?",
                (url, f"document_{key}_url", expected),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE bot_settings SET value=? WHERE key=?", (url, expected_key)
                )
                if key == "license":
                    conn.execute(
                        "UPDATE bot_settings SET value=? WHERE key='license_url' AND value=?",
                        (url, expected),
                    )


async def legal_publication_worker():
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for attempt in range(3):
            try:
                await publish_documents(client)
                return
            except Exception as exc:
                log.warning(
                    "Legal Telegraph publication unavailable (%s); current documents remain available",
                    type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(60)
