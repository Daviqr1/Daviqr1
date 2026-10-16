"""Garmin account bulk export ("Export Your Data" in Garmin's account settings) -> store.

The export is the best source for years of history: intraday cloud data older than ~6 months
is in cold storage, while the export holds every originally uploaded FIT file (Monitor, Sleep,
HRV status, Metrics, activities) inside nested zips under
DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_*_Part*.zip, plus JSON summaries.

FIT files are copied into the archive (the raw files are the source of truth) and decoded with
`importers.fit`; file names are generic, so files are classified by their FIT header, not name.
JSON: sleep summaries (sleep windows + score) and daily summaries (resting HR) are read tolerantly.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulso import metrics as M
from pulso import store
from pulso.importers import fit
from pulso.importers.garmin_connect import _gmt_ms
from pulso.store import Event

SRC = "gc:export"
MAX_NESTED = 2 * 1024**3  # refuse absurd members (zip bombs)


def _walk_zip(zf: zipfile.ZipFile, tmp: Path, depth: int = 0) -> Iterator[tuple[str, Path | bytes]]:
    """Yield (name, path_or_bytes) for every .fit and .json member, descending into nested zips
    (spilled to a temp file so multi-GB parts never sit in memory)."""
    for info in zf.infolist():
        name = info.filename
        low = name.lower()
        if info.is_dir() or info.file_size > MAX_NESTED:
            continue
        if low.endswith(".zip") and depth < 3:
            spill = tmp / f"nested_{depth}_{abs(hash(name))}.zip"
            with zf.open(info) as src, open(spill, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            try:
                with zipfile.ZipFile(spill) as inner:
                    yield from _walk_zip(inner, tmp, depth + 1)
            except zipfile.BadZipFile:
                pass
            finally:
                spill.unlink(missing_ok=True)
        elif low.endswith(".fit"):
            yield name, zf.read(info)
        elif low.endswith(".json") and ("sleep" in low or "udsfile" in low or "uds" in Path(low).name):
            yield name, zf.read(info)


def _iter_dir(root: Path, tmp: Path) -> Iterator[tuple[str, Path | bytes]]:
    for p in sorted(root.rglob("*")):
        low = p.name.lower()
        if p.is_file() and low.endswith(".zip"):
            try:
                with zipfile.ZipFile(p) as z:
                    yield from _walk_zip(z, tmp)
            except zipfile.BadZipFile:
                continue
        elif p.is_file() and low.endswith(".fit"):
            yield str(p), p
        elif p.is_file() and low.endswith(".json") and ("sleep" in low or "uds" in low):
            yield str(p), p.read_bytes()


def _sleep_json(conn: sqlite3.Connection, items: list) -> int:
    n = 0
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        s = it.get("sleepStartTimestampGMT")
        e = it.get("sleepEndTimestampGMT")
        s = int(s) if isinstance(s, (int, float)) else _gmt_ms(s)
        e = int(e) if isinstance(e, (int, float)) else _gmt_ms(e)
        if not s or not e or e <= s:
            continue
        scores = it.get("sleepScores") or {}
        overall = scores.get("overallScore", scores.get("overall"))
        if isinstance(overall, dict):
            overall = overall.get("value")
        store.upsert_event(conn, Event(f"gc:sleep:{s}", "sleep", s, e, "sono (Garmin)", SRC, {
            "score": overall, "deep_s": it.get("deepSleepSeconds"), "light_s": it.get("lightSleepSeconds"),
            "rem_s": it.get("remSleepSeconds"), "awake_s": it.get("awakeSleepSeconds"),
            "avg_stress": it.get("avgSleepStress"), "calendar_date": it.get("calendarDate")}))
        n += 1
    return n


def _uds_json(conn: sqlite3.Connection, items: list, tz: str) -> int:
    """Daily summaries: keep resting HR (a slow baseline the models use) at local noon."""
    n = 0
    zone = ZoneInfo(tz)
    rows = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("calendarDate"):
            continue
        rhr = it.get("restingHeartRate")
        try:
            d = datetime.fromisoformat(str(it["calendarDate"])[:10]).date()
        except ValueError:
            continue
        if rhr and 25 <= rhr <= 120:
            t = int(datetime.combine(d, time(12, 0), tzinfo=zone).astimezone(timezone.utc).timestamp() * 1000)
            rows.append((t, float(rhr)))
            n += 1
    store.insert_samples(conn, M.RESTING_HR, SRC, rows)
    return n


def import_export(conn: sqlite3.Connection, path: str | Path, tz: str, archive: str | Path | None = None,
                  log: Callable[[str], None] = print, batch: int = 200) -> dict:
    """Import a Garmin export zip (or the extracted folder). Returns counts."""
    path = Path(path)
    archive = Path(archive or os.environ.get("PULSO_ARCHIVE", "data/fit-archive")) / "export"
    archive.mkdir(parents=True, exist_ok=True)
    counts = {"fit_files": 0, "fit_new": 0, "sleep_nights": 0, "daily_summaries": 0, "errors": 0}
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        res = fit.import_paths(conn, pending)
        for v in res.values():
            if isinstance(v, dict):
                counts["fit_new"] += 1
            elif isinstance(v, str) and v.startswith("erro"):
                counts["errors"] += 1
        pending.clear()
        log(f"FIT: {counts['fit_files']} lidos, {counts['fit_new']} novos, {counts['errors']} com erro")

    with tempfile.TemporaryDirectory(prefix="pulso-export-") as td:
        tmp = Path(td)
        if path.is_dir():
            items = _iter_dir(path, tmp)
        else:
            zf = zipfile.ZipFile(path)
            items = _walk_zip(zf, tmp)
        for name, payload in items:
            low = name.lower()
            if low.endswith(".fit"):
                data = payload.read_bytes() if isinstance(payload, Path) else payload
                if len(data) < 14 or data[8:12] != b".FIT":
                    continue
                dest = archive / Path(name).name
                if dest.exists() and dest.read_bytes() != data:
                    dest = archive / f"{dest.stem}_{len(data)}{dest.suffix}"
                if not dest.exists():
                    dest.write_bytes(data)
                pending.append(str(dest))
                counts["fit_files"] += 1
                if len(pending) >= batch:
                    flush()
            else:
                try:
                    obj = json.loads(payload if isinstance(payload, bytes) else payload.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                with store.transaction(conn):
                    if "sleep" in low:
                        counts["sleep_nights"] += _sleep_json(conn, obj)
                    else:
                        counts["daily_summaries"] += _uds_json(conn, obj, tz)
        flush()
    return counts


def import_bytes(conn: sqlite3.Connection, blob: bytes, tz: str, archive: str | Path) -> dict:
    """Convenience for tests and uploads: import an export held in memory."""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(blob)
        p = f.name
    try:
        return import_export(conn, p, tz, archive=archive, log=lambda m: None)
    finally:
        os.unlink(p)
