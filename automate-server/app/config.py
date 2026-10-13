"""Mirrors src/config.js exactly: single place all env vars are read.

Loads `.env` from the automate-server/ repo root via python-dotenv, then
builds a mutable `config` object other modules read from at call time (not
just at import time) so tests can monkeypatch `config.data_dir` after import,
same as the Node version reading `config.dataDir` lazily inside store.js.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# app/config.py -> parent is app/, parent.parent is the automate-server/ repo root
# (src/config.js is one level deeper, at src/config.js, so its "../" == our "./").
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


def _bool_env(name: str, default: bool = True) -> bool:
    """Mirrors `(process.env.X ?? "true") !== "false"` — true unless literally "false"."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw != "false"


class OpenAIConfig:
    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY", "") or ""
        self.model = os.environ.get("OPENAI_MODEL") or "gpt-5.6-sol"


class Config:
    def __init__(self) -> None:
        self.port = int(os.environ.get("PORT") or 3000)
        self.openai = OpenAIConfig()
        self.automation_threshold = int(os.environ.get("AUTOMATION_THRESHOLD") or 30)
        self.keeper_stub = _bool_env("KEEPER_STUB", True)
        self.zendesk_stub = _bool_env("ZENDESK_STUB", True)
        # Overridable so tests can point at a throwaway directory instead of the real data/.
        data_dir_override = os.environ.get("AUTOMATE_DATA_DIR")
        self.data_dir = data_dir_override if data_dir_override else str(REPO_ROOT / "data") + os.sep
        self.fixtures_dir = str(REPO_ROOT / "fixtures") + os.sep

        # Consumed by app.pipeline / app.self_heal (another agent's slice); config.py stays
        # the single place all env vars are read, matching the Node original's pattern.
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "") or ""
        self.anthropic_model = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5"
        self.llm_provider = os.environ.get("LLM_PROVIDER") or "openai"


config = Config()


def has_live_openai() -> bool:
    """True once a real OpenAI key is configured; otherwise Stage 2 uses the mock generator."""
    return bool(config.openai.api_key)
