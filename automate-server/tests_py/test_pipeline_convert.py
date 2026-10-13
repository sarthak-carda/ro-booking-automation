"""Self-verification for this agent's slice: Stage 0 (context), Stage 1
validation (capture), Stage 2 (convert / LLM client for both OpenAI and
Anthropic).

(a) exercises the offline mock path end to end against the real
    fixtures/funnels/example-vendor.funnel.json + sample-booking-1.json,
    checking the generated script matches the 3-argument
    `findEl(candidates, stepIndex, action)` contract self-heal depends on.
(b) makes ONE real call through app.llm.client.complete (purpose="convert")
    against the live OPENAI_API_KEY configured in automate-server/.env --
    small, real cost, intentionally run once as a sanity check that the live
    wiring (no temperature field, correct URL/headers/model) actually works
    end to end against gpt-5.6-sol.

Run standalone: `.venv/bin/pytest tests_py/test_pipeline_convert.py -v`
"""
import json
import re
from pathlib import Path

import pytest

from app.pipeline import capture
from app.pipeline.context_builder import build_generation_request
from app.pipeline.convert import convert_funnel_to_script, extract_code_block
from app.llm import mock as llm_mock

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixture(rel_path: str):
    with open(FIXTURES_DIR / rel_path, "r", encoding="utf8") as f:
        return json.load(f)


FUNNEL = _load_fixture("funnels/example-vendor.funnel.json")
BOOKING = _load_fixture("bookings/sample-booking-1.json")

VENDOR = {
    "id": FUNNEL["vendorId"],
    "name": "Example Vendor",
    "portalUrl": FUNNEL["portalUrl"],
    "requiresCredentials": FUNNEL.get("loginRequired", False),
}


def test_fixture_funnel_is_a_valid_recording():
    """Sanity check on the fixture itself: it must pass capture's own validator."""
    assert capture.validate_funnel_recording(FUNNEL) is True


def test_capture_validate_rejects_bad_step_with_exact_message():
    bad = {"steps": [{"index": 0, "action": "bogus"}]}
    with pytest.raises(ValueError, match=r'Step 0: invalid action "bogus"'):
        capture.validate_funnel_recording(bad)


def test_capture_validate_rejects_fill_step_without_key_or_credentials():
    bad = {"steps": [{"index": 0, "action": "fill", "selector_candidates": ["#x"]}]}
    with pytest.raises(ValueError, match=r"Step 0: fill step needs field_to_booking_key or requires_credentials"):
        capture.validate_funnel_recording(bad)


def test_build_generation_request_shape():
    generation_request = build_generation_request(VENDOR, BOOKING)
    assert generation_request["vendorId"] == FUNNEL["vendorId"]
    assert generation_request["portalUrl"] == FUNNEL["portalUrl"]
    assert generation_request["bookingSchemaSample"] == BOOKING
    assert generation_request["requiresCredentials"] is False


def test_offline_mock_script_matches_findel_three_arg_contract():
    """(a) offline mock path produces a script matching the 3-arg findEl
    contract that a self-heal system elsewhere regex-patches by step."""
    script = llm_mock.generate_script_from_funnel(FUNNEL)

    assert "window.HeadoutAutomation = {" in script
    assert "function findEl(candidates, stepIndex, action)" in script
    # findEl throws its own error -- no separate hand-written "if (!elN) throw" line
    # anywhere, which would duplicate the candidates array as a second stale copy.
    assert "if (!el" not in script

    fillable_steps = [s for s in FUNNEL["steps"] if s["action"] in ("fill", "click", "submit")]
    assert len(fillable_steps) > 0

    for step in fillable_steps:
        i = step["index"]
        action = step["action"]
        candidates_literal = json.dumps(step["selector_candidates"], separators=(",", ":"), ensure_ascii=False)
        expected_call = f'const el{i} = findEl({candidates_literal}, {i}, "{action}");'
        assert expected_call in script, f"step {i} findEl call not found verbatim in generated script"

    # Exercised through mock_complete's "convert" dispatch (the real code path
    # app.llm.client.complete falls back to when no key is configured), not just
    # the generator function directly -- proves extract_json_payload's two-block
    # merge (booking schema + funnel) actually feeds `funnel` through.
    user_text = "\n".join([
        "```json",
        json.dumps(BOOKING, indent=2),
        "```",
        "```json",
        json.dumps({"funnel": FUNNEL}, indent=2),
        "```",
    ])
    raw = llm_mock.mock_complete("convert", [{"type": "text", "text": user_text}])
    assert raw.startswith("```js\n")
    mocked_script = extract_code_block(raw)
    assert "window.HeadoutAutomation" in mocked_script
    assert f'const el0 = findEl(' in mocked_script


@pytest.mark.asyncio
async def test_real_openai_convert_call_returns_non_mocked_script():
    """(b) a REAL call through app.llm.client.complete with purpose="convert"
    against the live OPENAI_API_KEY in .env. Small, real cost -- run once."""
    from app.config import config

    if not config.openai.api_key:
        pytest.skip("OPENAI_API_KEY not configured in automate-server/.env")
    if getattr(config, "llm_provider", "openai") != "openai":
        pytest.skip("LLM_PROVIDER is not 'openai' -- this test targets the OpenAI path specifically")

    generation_request = build_generation_request(VENDOR, BOOKING)
    result = await convert_funnel_to_script(generation_request, FUNNEL)

    assert result["mocked"] is False
    assert "window.HeadoutAutomation" in result["script"]
    assert re.search(r"findEl\(", result["script"])
