"""Your own commits as a behavioural signal.

Late-night commits, commit bursts before a deadline and weekend work are some of the most
honest stress markers a developer leaves behind. This reads `git log` of local repositories
and stores one 'commit' event per commit (author filtered), with size stats in attrs.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from pulso import store
from pulso.store import Event

SEP = "\x1f"


def import_repo(conn: sqlite3.Connection, repo: str | Path, author: str, since: str = "90 days ago") -> int:
    repo = Path(repo)
    fmt = SEP.join(["%H", "%aI", "%s"])
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--all", f"--author={author}", f"--since={since}",
         f"--pretty=format:{fmt}", "--shortstat", "--no-merges"],
        check=True, capture_output=True, text=True,
    ).stdout
    n = 0
    pending: dict | None = None
    rows: list[Event] = []

    def flush():
        nonlocal pending
        if pending:
            rows.append(Event(id=f"commit:{pending['sha']}", kind="commit", start_ms=pending["ms"], end_ms=pending["ms"],
                              title=pending["subject"][:200], source=f"git:{repo.name}",
                              attrs={"repo": repo.name, "files": pending.get("files", 0),
                                     "insertions": pending.get("ins", 0), "deletions": pending.get("dels", 0)}))
        pending = None

    from datetime import datetime

    for line in out.splitlines():
        if SEP in line:
            flush()
            sha, iso, subject = line.split(SEP, 2)
            pending = {"sha": sha, "ms": int(datetime.fromisoformat(iso).timestamp() * 1000), "subject": subject}
        elif line.strip() and pending is not None and "changed" in line:
            for part in line.split(","):
                part = part.strip()
                num = int(part.split()[0])
                if "file" in part:
                    pending["files"] = num
                elif "insertion" in part:
                    pending["ins"] = num
                elif "deletion" in part:
                    pending["dels"] = num
    flush()
    with store.transaction(conn):
        for ev in rows:
            store.upsert_event(conn, ev)
            n += 1
    return n
