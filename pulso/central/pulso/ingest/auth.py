"""Device bearer tokens. Only SHA-256 digests are stored; the prefix makes a leaked token easy
for secret scanners to recognise."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from pulso import store

PREFIX = "pulso_dev_"


def create_token(conn: sqlite3.Connection, label: str) -> str:
    tok = PREFIX + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO device_tokens (token_sha256, label, created_ms) VALUES (?, ?, ?)",
        (_digest(tok), label, store.now_ms()),
    )
    return tok


def revoke(conn: sqlite3.Connection, label: str) -> int:
    cur = conn.execute("UPDATE device_tokens SET revoked_ms = ? WHERE label = ? AND revoked_ms IS NULL",
                       (store.now_ms(), label))
    return cur.rowcount


def check(conn: sqlite3.Connection, header: str | None) -> str | None:
    """Return the token label when `Authorization: Bearer <tok>` is valid, else None."""
    if not header or not header.lower().startswith("bearer "):
        return None
    tok = header[7:].strip()
    if not tok.startswith(PREFIX):
        return None
    d = _digest(tok)
    row = conn.execute("SELECT token_sha256, label FROM device_tokens WHERE token_sha256 = ? AND revoked_ms IS NULL",
                       (d,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_sha256"], d):
        return None
    return row["label"]


def _digest(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()
