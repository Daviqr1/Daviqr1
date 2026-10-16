"""Runtime configuration from environment variables (12-factor; Docker/Coolify friendly)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("PULSO_DB", "data/pulso.sqlite")))
    tz: str = field(default_factory=lambda: os.environ.get("PULSO_TZ", "America/Sao_Paulo"))
    # Claude
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY"))
    report_model: str = field(default_factory=lambda: os.environ.get("PULSO_REPORT_MODEL", "claude-opus-5-5"))
    tagger_model: str = field(default_factory=lambda: os.environ.get("PULSO_TAGGER_MODEL", "claude-haiku-5-5"))
    # MCP over HTTP
    mcp_token: str | None = field(default_factory=lambda: os.environ.get("PULSO_MCP_TOKEN"))
    # how often the face asks for a recompute of the current day (seconds)
    reanalyze_after_s: int = field(default_factory=lambda: int(os.environ.get("PULSO_REANALYZE_S", "300")))


def get() -> Settings:
    return Settings()
