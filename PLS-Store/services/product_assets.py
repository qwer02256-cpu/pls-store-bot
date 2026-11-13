"""Validate plugin updates without importing or executing uploaded Python."""

import ast
import re
from pathlib import Path
from services.db import db
from services.licenses import audit


def valid_version(value):
    value = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+ -]{0,63}", value):
        raise ValueError("Укажите версию, например 2.3 или 2.3.1-beta")
    return value


def source_version(source):
    for node in ast.parse(source).body:
        names = (
            [n.id for n in node.targets if isinstance(n, ast.Name)]
            if isinstance(node, ast.Assign)
            else (
                [node.target.id]
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                else []
            )
        )
        if "VERSION" in names:
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                return None
            try:
                return valid_version(value)
            except ValueError:
                return None
    return None


def normalize_source(source, autostars=False):
    tree = ast.parse(source)
    if autostars:
        if "PLS_LICENSE_GUARD_V1" not in source:
            raise ValueError("Нужна версия AutoStars с проверкой лицензий PLS Store")
        endpoint = next(
            (
                n
                for n in tree.body
                if isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "_PLS_ENDPOINT"
                    for t in n.targets
                )
            ),
            None,
        )
        if (
            endpoint is None
            or not isinstance(endpoint.value, ast.Constant)
            or not isinstance(endpoint.value.value, str)
        ):
            raise ValueError("В AutoStars отсутствует адрес проверки лицензии")
        # A file downloaded by a buyer contains an embedded endpoint. Normalize it
        # back to a template; delivery always embeds the store's current origin.
        lines = source.splitlines(keepends=True)
        lines[endpoint.lineno - 1 : endpoint.end_lineno] = [
            '_PLS_ENDPOINT = "__PLS_LICENSE_ENDPOINT__"\n'
        ]
        source = "".join(lines)
        ast.parse(source)
    return source


def safe_filename(name):
    if (
        not name
        or len(name) > 120
        or Path(name).name != name
        or any(c in name for c in ("\\", "\0", "\r", "\n"))
        or not name.endswith(".py")
    ):
        raise ValueError("Нужно корректное имя файла .py")
    return name


def publish_file(pid, path, version, admin_id):
    version = valid_version(version)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise ValueError("Товар не найден")
        old = conn.execute(
            "SELECT * FROM license_assets WHERE product_id=?", (pid,)
        ).fetchone()
        conn.execute(
            "INSERT INTO license_assets(product_id,file_path,instruction) VALUES(?,?,?) ON CONFLICT(product_id) DO UPDATE SET file_path=excluded.file_path",
            (pid, str(path), old["instruction"] if old else ""),
        )
        conn.execute("UPDATE products SET version=? WHERE id=?", (version, pid))
        audit(conn, None, admin_id, f"asset:{pid}:file")
        audit(conn, None, admin_id, f"version:{pid}:{version}")
