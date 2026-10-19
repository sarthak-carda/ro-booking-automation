"""Thin LLM client used by Stage 2 (script conversion) and the self-heal
drift-check agent. Ports src/openai/client.js, extended to support BOTH
OpenAI and Anthropic as the backend (chosen by `config.llm_provider`) since
this Python port needs to exercise both. Falls back to a deterministic mock
when no key is configured for the selected provider, so the rest of the
pipeline (audit, ship, self-heal, threshold) is provable without a live key.
"""
from typing import Any

import httpx

from app.config import config

try:
    from app.config import has_live_llm
except ImportError:  # app.config hasn't grown a provider-aware gate yet — stub locally,
    # reconciled once app/config.py (another agent's slice) adds the real one.
    def has_live_llm() -> bool:
        provider = getattr(config, "llm_provider", "openai")
        if provider == "anthropic":
            return bool(getattr(config, "anthropic_api_key", ""))
        return bool(config.openai.api_key)

from app.llm.mock import mock_complete

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


async def complete(system: str, user_content: Any, purpose: str) -> dict:
    """@param system - system prompt
    @param user_content - list of content blocks, e.g. [{"type": "text", "text": "..."}]
    @param purpose - "convert" | "drift-check" — used by the mock to pick a canned response
    @returns {"text": str, "mocked": bool}
    """
    if not has_live_llm():
        return {"text": mock_complete(purpose, user_content), "mocked": True}

    provider = getattr(config, "llm_provider", "openai")
    if provider == "anthropic":
        return await _complete_anthropic(system, user_content)
    return await _complete_openai(system, user_content)


async def _complete_openai(system: str, user_content: Any) -> dict:
    async with httpx.AsyncClient(timeout=180) as client:
        res = await client.post(
            OPENAI_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.openai.api_key}",
            },
            json={
                "model": config.openai.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                # No temperature override: newer reasoning-tier models (e.g. gpt-5.6-sol,
                # confirmed live) reject any value other than the default (1) with a 400.
                # Omitting it works for both reasoning and classic chat models.
            },
        )

    if res.status_code < 200 or res.status_code >= 300:
        body = res.text
        raise RuntimeError(f"OpenAI API returned {res.status_code}: {body[:500]}")

    data = res.json()
    text = None
    choices = data.get("choices") or []
    if choices:
        text = (choices[0].get("message") or {}).get("content")
    if not text:
        raise RuntimeError("OpenAI response had no message content")
    return {"text": text, "mocked": False}


async def _complete_anthropic(system: str, user_content: Any) -> dict:
    from anthropic import AsyncAnthropic  # imported lazily so the OpenAI path never needs the SDK installed

    client = AsyncAnthropic(api_key=config.anthropic_api_key)
    try:
        # Streamed, not a plain `messages.create`: the SDK refuses a non-streaming call whose
        # max_tokens implies a >10-minute request, and a full multi-page vendor script
        # (~10K tokens of output, measured on a real 34KB Prado script) plus the model's
        # own thinking needs more headroom than the old 16000 that fit single-page scripts.
        # `get_final_message()` returns the same Message object `create` would have.
        async with client.messages.stream(
            model=config.anthropic_model,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            # Real bug found 2026-09-23: claude-sonnet-5 used extended thinking by
            # default on this prompt and consumed the ENTIRE max_tokens=4096 budget on
            # thinking alone (output_tokens_details.thinking_tokens=4096, stop_reason
            # "max_tokens"), leaving zero tokens for the actual text/code response.
            # Thinking tokens count against max_tokens, so this needs real headroom
            # for both, not just the expected size of the final script/JSON.
            max_tokens=32000,
        ) as stream:
            response = await stream.get_final_message()
    except Exception as err:  # anthropic.APIStatusError and friends carry the body on str(err)
        raise RuntimeError(f"Anthropic API returned an error: {str(err)[:500]}") from err

    # A truncated script is syntactically broken JS that would only surface later as a
    # confusing audit failure (and could be handed to a repair pass as if it were a real
    # script bug). Fail here, where the real cause is visible.
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Anthropic response was cut off at max_tokens before finishing "
            f"(usage={getattr(response, 'usage', None)!r}) — the output would be truncated."
        )

    text = "".join(
        block.text for block in (response.content or []) if getattr(block, "type", None) == "text"
    )
    if not text:
        block_types = [getattr(b, "type", type(b).__name__) for b in (response.content or [])]
        raise RuntimeError(
            f"Anthropic response had no text content. stop_reason={response.stop_reason!r}, "
            f"content block types={block_types!r}, usage={getattr(response, 'usage', None)!r}"
        )
    return {"text": text, "mocked": False}
