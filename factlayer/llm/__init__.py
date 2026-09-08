"""LLM access behind one tiny interface so the backend is swappable.

    from factlayer.llm import complete_json
    data = await complete_json(system, user, schema, tier="fast", purpose="extract", doc_id=...)

Backends (FACTLAYER_LLM_BACKEND):
  claude_cli  - shells out to the local `claude -p` (Claude Code CLI). Uses whatever login the CLI has;
                no API key in the repo. This is what the demo was run with.
  anthropic   - official Anthropic SDK with structured outputs. Needs ANTHROPIC_API_KEY or `ant auth login`.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .. import config, db

_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.LLM_CONCURRENCY)
    return _sem


def model_for(tier: str) -> str:
    return config.MODEL_SMART if tier == "smart" else config.MODEL_FAST


class LLMError(RuntimeError):
    pass


async def complete_json(system: str, user: str, schema: dict[str, Any], *, tier: str = "fast",
                        purpose: str = "generic", doc_id: str | None = None, retries: int = 1) -> dict[str, Any]:
    """Return a dict validated against `schema` by the backend. Logs every attempt to llm_calls."""
    if config.LLM_BACKEND == "anthropic":
        from . import anthropic_api as backend
    else:
        from . import claude_cli as backend
    model = model_for(tier)
    last: Exception | None = None
    for attempt in range(retries + 1):
        async with _semaphore():
            try:
                data, usage = await backend.complete_json(system, user, schema, model)
                db.log_llm_call(doc_id, purpose, usage.get("model", model), True, usage.get("duration_ms"),
                                usage.get("cost_usd"), usage.get("input_tokens"), usage.get("output_tokens"))
                return data
            except Exception as e:  # noqa: BLE001 - we log and retry once, then surface
                last = e
                db.log_llm_call(doc_id, purpose, model, False, None, None, None, None, error=str(e)[:500])
                if attempt < retries:
                    await asyncio.sleep(2 + 3 * attempt)
    raise LLMError(f"{purpose} failed after {retries + 1} attempts: {last}")
