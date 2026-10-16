"""SQLite access layer.

One file, WAL mode, so the ingest API, the importers, the analysis jobs and the MCP server
can all read concurrently while one process writes. Every write is idempotent on the
natural key, so re-importing the same FIT file or retrying an upload is harmless.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = 1


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the store, creating and migrating it if needed."""
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    sql = resources.files("pulso.store").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    if row["v"] is None:
        conn.execute(
            "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
            (SCHEMA_VERSION, now_ms()),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------- writes


def insert_samples(
    conn: sqlite3.Connection, metric: str, source: str, rows: Iterable[tuple[int, float]]
) -> int:
    """Insert (ts_ms, value) rows for one metric/source. Returns rows actually inserted."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO samples (metric, ts_ms, source, value) VALUES (?, ?, ?, ?)",
        ((metric, int(ts), source, float(v)) for ts, v in rows if v is not None),
    )
    return conn.total_changes - before


def upsert_samples(
    conn: sqlite3.Connection, metric: str, source: str, rows: Iterable[tuple[int, float]]
) -> int:
    """Like insert_samples but replaces existing values (for derived series that get recomputed)."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR REPLACE INTO samples (metric, ts_ms, source, value) VALUES (?, ?, ?, ?)",
        ((metric, int(ts), source, float(v)) for ts, v in rows if v is not None),
    )
    return conn.total_changes - before


def insert_rr(
    conn: sqlite3.Connection,
    source: str,
    rows: Iterable[tuple[int, int]],
    session_id: str | None = None,
) -> int:
    """Insert (beat_ts_ms, rr_ms) rows."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO rr_intervals (source, ts_ms, rr_ms, session_id) VALUES (?, ?, ?, ?)",
        ((source, int(ts), int(rr), session_id) for ts, rr in rows),
    )
    return conn.total_changes - before


def insert_motion_chunk(
    conn: sqlite3.Connection,
    source: str,
    sensor: str,
    t0_ms: int,
    rate_hz: float,
    x: Sequence[int],
    y: Sequence[int],
    z: Sequence[int],
    session_id: str | None = None,
) -> int:
    n = len(x)
    if not (len(y) == n and len(z) == n):
        raise ValueError("motion axes must have equal length")
    pack = lambda a: np.asarray(a, dtype="<i2").tobytes()  # noqa: E731
    before = conn.total_changes
    conn.execute(
        "INSERT OR IGNORE INTO motion_chunks (source, sensor, t0_ms, rate_hz, n, x, y, z, session_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, sensor, int(t0_ms), float(rate_hz), n, pack(x), pack(y), pack(z), session_id),
    )
    return conn.total_changes - before


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    start_ms: int
    end_ms: int
    title: str | None
    source: str
    attrs: dict[str, Any]


def upsert_event(conn: sqlite3.Connection, ev: Event) -> None:
    conn.execute(
        "INSERT INTO events (id, kind, start_ms, end_ms, title, source, attrs) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, start_ms=excluded.start_ms,"
        " end_ms=excluded.end_ms, title=excluded.title, source=excluded.source, attrs=excluded.attrs",
        (ev.id, ev.kind, ev.start_ms, ev.end_ms, ev.title, ev.source, json.dumps(ev.attrs, ensure_ascii=False)),
    )


def put_artefact(conn: sqlite3.Connection, kind: str, key: str, body: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO artefacts (kind, key, created_ms, body) VALUES (?, ?, ?, ?)",
        (kind, key, now_ms(), json.dumps(body, ensure_ascii=False, default=_json_default)),
    )


def get_artefact(conn: sqlite3.Connection, kind: str, key: str) -> Any | None:
    row = conn.execute("SELECT body FROM artefacts WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    return None if row is None else json.loads(row["body"])


def _json_default(o: Any) -> Any:
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


# ----------------------------------------------------------------------------- reads


def series(
    conn: sqlite3.Connection,
    metric: str,
    start_ms: int,
    end_ms: int,
    source: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (ts_ms int64, value float64) for a metric in [start_ms, end_ms), time-sorted.

    When `source` is None and several sources report the same metric, all are returned
    (callers that need one source per timestamp should pass `source`).
    """
    if source is None:
        rows = conn.execute(
            "SELECT ts_ms, value FROM samples WHERE metric = ? AND ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
            (metric, start_ms, end_ms),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts_ms, value FROM samples WHERE metric = ? AND source = ? AND ts_ms >= ? AND ts_ms < ?"
            " ORDER BY ts_ms",
            (metric, source, start_ms, end_ms),
        ).fetchall()
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    arr = np.asarray([(r[0], r[1]) for r in rows], dtype=np.float64)
    return arr[:, 0].astype(np.int64), arr[:, 1]


def rr(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, source: str | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Return (beat_ts_ms int64, rr_ms float64) in [start_ms, end_ms), time-sorted."""
    q = "SELECT ts_ms, rr_ms FROM rr_intervals WHERE ts_ms >= ? AND ts_ms < ?"
    args: list[Any] = [start_ms, end_ms]
    if source is not None:
        q += " AND source = ?"
        args.append(source)
    rows = conn.execute(q + " ORDER BY ts_ms", args).fetchall()
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    arr = np.asarray([(r[0], r[1]) for r in rows], dtype=np.float64)
    return arr[:, 0].astype(np.int64), arr[:, 1]


def motion(
    conn: sqlite3.Connection, sensor: str, start_ms: int, end_ms: int, source: str | None = None
) -> Iterator[tuple[int, float, np.ndarray]]:
    """Yield (t0_ms, rate_hz, xyz[n,3] int16) chunks overlapping [start_ms, end_ms)."""
    q = "SELECT t0_ms, rate_hz, n, x, y, z FROM motion_chunks WHERE sensor = ? AND t0_ms < ? AND t0_ms >= ?"
    # chunks are short (<= a few minutes); look back 10 min so overlapping chunks are included
    args: list[Any] = [sensor, end_ms, start_ms - 600_000]
    if source is not None:
        q += " AND source = ?"
        args.append(source)
    for r in conn.execute(q + " ORDER BY t0_ms", args):
        n = r["n"]
        xyz = np.empty((n, 3), dtype=np.int16)
        xyz[:, 0] = np.frombuffer(r["x"], dtype="<i2", count=n)
        xyz[:, 1] = np.frombuffer(r["y"], dtype="<i2", count=n)
        xyz[:, 2] = np.frombuffer(r["z"], dtype="<i2", count=n)
        end_chunk = r["t0_ms"] + int(1000 * n / r["rate_hz"])
        if end_chunk > start_ms:
            yield r["t0_ms"], r["rate_hz"], xyz


def events(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, kinds: Sequence[str] | None = None
) -> list[Event]:
    """Events overlapping [start_ms, end_ms)."""
    q = "SELECT * FROM events WHERE start_ms < ? AND end_ms >= ?"
    args: list[Any] = [end_ms, start_ms]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    return [
        Event(r["id"], r["kind"], r["start_ms"], r["end_ms"], r["title"], r["source"], json.loads(r["attrs"]))
        for r in conn.execute(q + " ORDER BY start_ms", args)
    ]


def metrics_available(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Inventory: which metric/source pairs exist and their time coverage."""
    rows = conn.execute(
        "SELECT metric, source, COUNT(*) AS n, MIN(ts_ms) AS first_ms, MAX(ts_ms) AS last_ms"
        " FROM samples GROUP BY metric, source ORDER BY metric, source"
    ).fetchall()
    out = [dict(r) for r in rows]
    rr_row = conn.execute(
        "SELECT source, COUNT(*) AS n, MIN(ts_ms) AS first_ms, MAX(ts_ms) AS last_ms FROM rr_intervals GROUP BY source"
    ).fetchall()
    out.extend({"metric": "rr", **dict(r)} for r in rr_row)
    return out
