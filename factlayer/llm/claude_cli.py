"""`claude -p` backend. One subprocess per call, structured output enforced with --json-schema.

Notes learned the hard way:
  * CLAUDECODE must be unset or the CLI refuses to start inside another Claude Code session.
  * --bare skips credential loading, so it is NOT used.
  * --tools "" turns the call into a plain completion (no tool use, no filesystem access).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

from .. import config


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN"):
        env.pop(k, None)
    return env


async def complete_json(system: str, user: str, schema: dict[str, Any], model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` CLI not found on PATH; install Claude Code or set FACTLAYER_LLM_BACKEND=anthropic")
    args = [exe, "-p", "--no-session-persistence", "--tools", "", "--model", model,
            "--output-format", "json", "--json-schema", json.dumps(schema),
            "--system-prompt", system, user]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_env(), cwd=str(config.ROOT),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=config.LLM_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"claude -p timed out after {config.LLM_TIMEOUT_S}s")
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"claude -p exit {proc.returncode}: {err.decode(errors='replace')[:400]}")
    try:
        payload = json.loads(out.decode())
    except ValueError:
        raise RuntimeError(f"claude -p returned non-JSON: {out[:300]!r} {err[:200]!r}")
    if payload.get("is_error"):
        raise RuntimeError(f"claude -p error: {payload.get('result')}")
    data = payload.get("structured_output")
    if data is None:
        # Fallback: the model answered in text; try to parse it as JSON.
        try:
            data = json.loads(payload.get("result") or "")
        except ValueError:
            raise RuntimeError(f"no structured_output; result={str(payload.get('result'))[:300]!r}")
    usage = payload.get("usage") or {}
    model_used = next(iter((payload.get("modelUsage") or {}).keys()), model)
    return data, {
        "model": model_used,
        "duration_ms": payload.get("duration_api_ms") or payload.get("duration_ms"),
        "cost_usd": payload.get("total_cost_usd"),
        "input_tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens"),
    }
