"""Anthropic SDK backend (structured outputs via output_config.format). Same contract as claude_cli."""
from __future__ import annotations

import json
import time
from typing import Any

import anthropic

_client: anthropic.AsyncAnthropic | None = None


def _get() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile
    return _client


async def complete_json(system: str, user: str, schema: dict[str, Any], model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    t0 = time.time()
    async with _get().messages.stream(
        model=model,
        max_tokens=32000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    ) as stream:
        msg = await stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("model refused the request")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    data = json.loads(text)
    u = msg.usage
    return data, {
        "model": msg.model,
        "duration_ms": int((time.time() - t0) * 1000),
        "cost_usd": None,
        "input_tokens": (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
                        + (getattr(u, "cache_creation_input_tokens", 0) or 0),
        "output_tokens": u.output_tokens,
    }
