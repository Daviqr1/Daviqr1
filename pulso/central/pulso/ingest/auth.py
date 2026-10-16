"""Device bearer tokens. Only SHA-256 digests are stored; the prefix makes a leaked token easy
for secret scanners to recognise.

Two scopes, so the credential that has to live on the watch (baked into a sideloaded .prg, in the
Garmin Connect app settings) cannot read the whole history back:

    device   watch apps and the BLE bridge: POST /v1/ingest, GET /v1/face (the default; every
             token created before scopes existed is one)
    painel   the web dashboard: read-only GET /v1/painel/*; useless for uploads
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from pulso import store

PREFIX = "pulso_dev_"
DEVICE = "device"
PAINEL = "painel"
SCOPES = (DEVICE, PAINEL)


def create_token(conn: sqlite3.Connection, label: str, scope: str = DEVICE) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown token scope {scope!r}")
    tok = PREFIX + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO device_tokens (token_sha256, label, created_ms, scope) VALUES (?, ?, ?, ?)",
        (_digest(tok), label, store.now_ms(), scope),
    )
    return tok


def revoke(conn: sqlite3.Connection, label: str) -> int:
    cur = conn.execute("UPDATE device_tokens SET revoked_ms = ? WHERE label = ? AND revoked_ms IS NULL",
                       (store.now_ms(), label))
    return cur.rowcount


def lookup(conn: sqlite3.Connection, header: str | None) -> tuple[str, str] | None:
    """Return (label, scope) when `Authorization: Bearer <tok>` names a live token, else None."""
    if not header or not header.lower().startswith("bearer "):
        return None
    tok = header[7:].strip()
    if not tok.startswith(PREFIX):
        return None
    d = _digest(tok)
    row = conn.execute("SELECT token_sha256, label, scope FROM device_tokens WHERE token_sha256 = ? AND revoked_ms IS NULL",
                       (d,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_sha256"], d):
        return None
    return row["label"], row["scope"]


def check(conn: sqlite3.Connection, header: str | None, scope: str = DEVICE) -> str | None:
    """Return the token label when the header carries a live token of `scope`, else None."""
    hit = lookup(conn, header)
    return hit[0] if hit is not None and hit[1] == scope else None


def mac_keys(conn: sqlite3.Connection) -> list[bytes]:
    """BLE frame MAC keys: SHA-256 of each live *device* token. A painel token sits in a browser's
    localStorage and is read-only, so it must not be able to sign uploads over BLE either."""
    rows = conn.execute("SELECT token_sha256 FROM device_tokens WHERE revoked_ms IS NULL AND scope = ?",
                        (DEVICE,)).fetchall()
    return [bytes.fromhex(r[0]) for r in rows]


def _digest(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()
