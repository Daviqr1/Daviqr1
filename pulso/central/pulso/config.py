"""Runtime configuration from environment variables (12-factor; Docker/Coolify friendly)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    """Where the central keeps its data when nothing else is configured: ~/.pulso (notebook mode).
    Docker sets PULSO_DB / PULSO_ARCHIVE explicitly to /data."""
    return Path(os.environ.get("PULSO_HOME", Path.home() / ".pulso")).expanduser()


def ensure_home(create: bool = True) -> Path:
    """Make home() owner-only, wherever the database lives: it holds the FIT archive, the summaries and
    the Google login. Creates it 0700 before anything writes there, or tightens one an older version
    left 0755. Best effort (a read-only HOME fails later, where something actually writes); never
    touches $HOME or / when PULSO_HOME points there."""
    h = home()
    try:
        if create:
            h.mkdir(parents=True, exist_ok=True, mode=0o700)
        if h.exists() and h.resolve() not in (Path.home().resolve(), Path("/")):
            os.chmod(h, 0o700)
    except OSError:  # not ours (a bind mount owned by root) or not creatable
        pass
    return h


def archive_dir() -> Path:
    return Path(os.environ.get("PULSO_ARCHIVE", home() / "fit-archive")).expanduser()


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("PULSO_DB", home() / "pulso.sqlite")).expanduser())
    tz: str = field(default_factory=lambda: os.environ.get("PULSO_TZ", "America/Sao_Paulo"))
    # Claude
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY"))
    report_model: str = field(default_factory=lambda: os.environ.get("PULSO_REPORT_MODEL", "claude-opus-5-5"))
    # tagging is many small requests: same model at low effort by default; override to trade quality for cost
    tagger_model: str = field(default_factory=lambda: os.environ.get("PULSO_TAGGER_MODEL", "claude-opus-5-5"))
    # MCP over HTTP
    mcp_token: str | None = field(default_factory=lambda: os.environ.get("PULSO_MCP_TOKEN"))
    # how often the face asks for a recompute of the current day (seconds)
    reanalyze_after_s: int = field(default_factory=lambda: int(os.environ.get("PULSO_REANALYZE_S", "300")))


def get() -> Settings:
    return Settings()
