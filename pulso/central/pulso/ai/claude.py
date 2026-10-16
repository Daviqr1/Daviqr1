"""Thin wrapper over the Anthropic SDK for the two things Pulso asks Claude to do: tag transcript
segments and write reports — both as JSON constrained by a schema (structured outputs).

* Opus 5.5 by default: thinking is always on there, depth is set with `output_config.effort`.
* Server-side refusal fallback (`fallbacks: "default"`) is on, so a classifier false positive
  on health/stress text does not silently drop a report; `stop_reason == "refusal"` is still
  checked because the fallback model can also decline.
* No retries of our own: the SDK already retries 408/409/429/5xx and connection errors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pulso import config

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# models that accept server-side refusal fallbacks (Haiku 5.5 does not: no fallback exists for it)
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")


class ClaudeUnavailable(RuntimeError):
    pass


class ClaudeRefused(RuntimeError):
    def __init__(self, category: str | None, explanation: str | None):
        super().__init__(f"refused ({category}): {explanation}")
        self.category = category
        self.explanation = explanation


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


def _client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise ClaudeUnavailable("pip install 'pulso[ai]' para usar o Claude") from e
    return anthropic.Anthropic()


def structured(system: str, user: str, schema: dict[str, Any], *, model: str | None = None,
               effort: str = "high", max_tokens: int = 16000,
               settings: config.Settings | None = None, client: Any = None) -> tuple[dict, Usage]:
    """One request, JSON out. Returns (parsed_json, usage)."""
    import anthropic

    settings = settings or config.get()
    client = client or _client()
    model = model or settings.report_model
    params: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        cache_control={"type": "ephemeral"},
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    try:
        if model in FALLBACK_MODELS:
            resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
        else:
            resp = client.messages.create(**params)
    except anthropic.AuthenticationError as e:
        raise ClaudeUnavailable("credencial do Claude inválida ou ausente (ANTHROPIC_API_KEY / ant auth login)") from e
    except anthropic.APIConnectionError as e:
        raise ClaudeUnavailable(f"sem conexão com a API do Claude: {e}") from e

    if resp.stop_reason == "refusal":
        sd = getattr(resp, "stop_details", None)
        raise ClaudeRefused(getattr(sd, "category", None), getattr(sd, "explanation", None))
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("resposta cortada em max_tokens; aumente max_tokens ou reduza a entrada")
    # content may start with (empty) thinking blocks: pick the text block by type
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise RuntimeError("resposta sem bloco de texto")
    u = resp.usage
    usage = Usage(model=resp.model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                  cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
    return json.loads(text), usage
