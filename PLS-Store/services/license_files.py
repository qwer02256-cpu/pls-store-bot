"""Validate assets before charging or delivering. Never execute uploaded Python."""

import ast
import json
from pathlib import Path
from urllib.parse import urlsplit
from config import LICENSE_PUBLIC_BASE

ROOT = Path(__file__).resolve().parents[1]


def plugin_bytes(file_path):
    if not file_path:
        raise ValueError("Файл плагина не загружен")
    path = Path(file_path)
    if not path.is_absolute():
        path = ROOT / path
    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    if path.name == "AutoStars.py":
        base = urlsplit(LICENSE_PUBLIC_BASE)
        if (
            base.scheme != "https"
            or not base.hostname
            or base.username
            or base.password
            or base.query
            or base.fragment
            or base.path not in ("", "/")
        ):
            raise ValueError("LICENSE_PUBLIC_BASE должен быть публичным HTTPS origin")
        if (
            "PLS_LICENSE_GUARD_V1" not in source
            or '"__PLS_LICENSE_ENDPOINT__"' not in source
        ):
            raise ValueError("AutoStars без проверки лицензий")
        source = source.replace(
            '"__PLS_LICENSE_ENDPOINT__"',
            json.dumps(LICENSE_PUBLIC_BASE + "/licenses/verify"),
        )
        ast.parse(source)
    return path.name, source.encode("utf-8")


def ready(conn, pid):
    row = conn.execute(
        "SELECT * FROM license_assets WHERE product_id=?", (pid,)
    ).fetchone()
    if not row or not row["instruction"].strip():
        return False
    try:
        plugin_bytes(row["file_path"])
    except (ValueError, OSError, SyntaxError, UnicodeError):
        return False
    return True
