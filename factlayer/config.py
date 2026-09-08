"""Runtime settings. Everything is overridable from the environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("FACTLAYER_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("FACTLAYER_DB", DATA_DIR / "factlayer.db"))

# LLM backend: "claude_cli" (uses the local `claude` CLI login) or "anthropic" (API key / ant profile)
LLM_BACKEND = os.getenv("FACTLAYER_LLM_BACKEND", "claude_cli")
# Two tiers: "fast" does bulk extraction, "smart" does cross-document reasoning.
MODEL_FAST = os.getenv("FACTLAYER_MODEL_FAST", "sonnet" if LLM_BACKEND == "claude_cli" else "claude-sonnet-5")
MODEL_SMART = os.getenv("FACTLAYER_MODEL_SMART", "opus" if LLM_BACKEND == "claude_cli" else "claude-opus-5")
LLM_CONCURRENCY = int(os.getenv("FACTLAYER_LLM_CONCURRENCY", "6"))
LLM_TIMEOUT_S = int(os.getenv("FACTLAYER_LLM_TIMEOUT_S", "900"))

# Chunking: consecutive pages are packed until this many characters.
CHUNK_CHARS = int(os.getenv("FACTLAYER_CHUNK_CHARS", "18000"))
MAX_FACTS_PER_CHUNK = int(os.getenv("FACTLAYER_MAX_FACTS_PER_CHUNK", "40"))

# Linking
EMBED_MODEL = os.getenv("FACTLAYER_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
SIM_THRESHOLD = float(os.getenv("FACTLAYER_SIM_THRESHOLD", "0.82"))
MAX_NEIGHBOURS = int(os.getenv("FACTLAYER_MAX_NEIGHBOURS", "8"))
MAX_CLUSTER = int(os.getenv("FACTLAYER_MAX_CLUSTER", "10"))
NUMERIC_TOLERANCE = float(os.getenv("FACTLAYER_NUMERIC_TOLERANCE", "0.01"))  # 1 % => same number

for _d in (DATA_DIR, UPLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)
