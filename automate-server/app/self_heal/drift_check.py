"""Drift-check agent. Ports src/selfHeal/driftCheck.js exactly.

Given the failing audit's error (which names the failed step, per
convert.py's CONTRACT §9 / app/llm/mock.py's generated `findEl` helper) plus
the reference DOM from Stage 1's original capture and a fresh DOM from the
failing run, diagnoses what changed on the vendor's page.
"""
import json
import re
from typing import Any, Optional

from app.llm.client import complete

# This EXACT format is produced by Stage 2's port (app/llm/mock.py's generated
# `findEl` helper, and the real-model contract in app/pipeline/convert.py's
# system prompt) — do not change this regex to something "cleaner", it must
# match the error string byte for byte.
STEP_ERROR_RE = re.compile(r"^Step (\d+) \(([\w-]+)\): no element matched candidates (\[.*\])$")


def parse_failing_step(error_message: Any) -> Optional[dict]:
    """Pulls the failing step index out of an audit error message, if it
    matches the known format. Returns None (not selector-shaped) otherwise."""
    match = STEP_ERROR_RE.match(str(error_message) if error_message is not None else "")
    if not match:
        return None
    return {
        "step_index": int(match.group(1)),
        "action": match.group(2),
        "tried_candidates": json.loads(match.group(3)),
    }


# Reference/current DOM snapshots can easily run to hundreds of KB on a real
# checkout page — that's cost and noise the model doesn't need, and on a large
# enough vendor page it risks pushing the prompt past a useful context budget.
# If referenceDom/currentDom aren't already pruned to the relevant region
# upstream (wherever the DOM snapshot is captured), truncate defensively here
# rather than silently sending an oversized, unbounded payload.
MAX_DOM_CHARS = 40_000


def _truncate_dom(dom: Any) -> Any:
    if not isinstance(dom, str) or len(dom) <= MAX_DOM_CHARS:
        return dom
    return dom[:MAX_DOM_CHARS] + f"\n<!-- truncated at {MAX_DOM_CHARS} chars -->"


SYSTEM_PROMPT = (
    "You are diagnosing why a browser automation script can no longer find an element on a "
    "vendor's checkout page. Compare the reference DOM (captured when the script was written) "
    "against the current DOM (captured at the point of failure). failingSelectors are the "
    "candidates that matched during the original audit and no longer match now — use them to "
    "locate where that field/button likely moved to in the current DOM. The step index is "
    "already known (failingStepIndex) and is not yours to redetermine — do not include it in "
    "your answer. Respond with ONLY a JSON object: "
    '{ "diagnosis": "<one sentence>", "suggestedSelectors": ["<css selector>", ...] }'
)


def _extract_json(text: str) -> str:
    match = re.search(r"```json[ \t]*\r?\n([\s\S]*?)```", text)
    return match.group(1) if match else text


async def diagnose_drift(
    reference_dom: str,
    current_dom: str,
    failing_step_index: int,
    failing_selectors: Optional[list] = None,
) -> dict:
    """@param reference_dom - the DOM snapshot captured for this step during Stage 1
    @param current_dom - a fresh DOM snapshot taken at the point of failure
    @param failing_step_index
    @param failing_selectors - the selector candidates the failing step's `findEl`
      currently tries (from parse_failing_step), which matched at Stage 3 audit
      time but no longer match
    @returns {"step_index": int, "diagnosis": str, "suggested_selectors": list[str], "mocked": bool}

    Deliberately fails loudly (raises) if the model returns no diagnosis or no
    suggestedSelectors — this is NOT a bug to paper over with a fallback; a
    self-heal attempt with nothing to patch with is a genuine failed attempt,
    which the caller (self_heal/loop.py) records and lets the retry cap turn
    into an escalation.
    """
    failing_selectors = failing_selectors or []
    payload = {
        "referenceDom": _truncate_dom(reference_dom),
        "currentDom": _truncate_dom(current_dom),
        "failingStepIndex": failing_step_index,
        "failingSelectors": failing_selectors,
    }

    result = await complete(
        system=SYSTEM_PROMPT,
        user_content=[{"type": "text", "text": "```json\n" + json.dumps(payload, indent=2) + "\n```"}],
        purpose="drift-check",
    )
    text = result["text"]
    mocked = result["mocked"]
    parsed = json.loads(_extract_json(text))

    diagnosis = parsed.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise ValueError(f"diagnose_drift: model returned no diagnosis for step {failing_step_index}")

    suggested_selectors = parsed.get("suggestedSelectors")
    if not isinstance(suggested_selectors, list) or len(suggested_selectors) == 0:
        raise ValueError(f"diagnose_drift: model returned no suggestedSelectors for step {failing_step_index}")

    # step_index is deliberately the input, not whatever (if anything) the model
    # echoed back — it's already known precisely from the audit error, so
    # there's nothing for the model to add here.
    return {
        "step_index": failing_step_index,
        "diagnosis": diagnosis,
        "suggested_selectors": suggested_selectors,
        "mocked": mocked,
    }
