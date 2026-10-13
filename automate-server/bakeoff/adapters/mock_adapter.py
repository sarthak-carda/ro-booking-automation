"""
Not a real capture tool — returns the hand-authored example-vendor funnel
fixture as-is. Exists purely to prove the harness (dispatch -> scorecard ->
Stage 2/3 pipeline) actually works end to end before any real tool is wired
up. Always reports status "ok".

Port of bakeoff/adapters/mockAdapter.mjs. Called directly in-process by
run_bakeoff.py (see bakeoff/lib/run_adapter.py's module docstring for why
this one doesn't go through the subprocess protocol).
"""
import json
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures"


async def run(input: dict) -> dict:  # noqa: A002 - matches the other adapters' `input` param name
    del input  # input isn't used — always returns the same fixture, same as mockAdapter.mjs
    funnel = json.loads((FIXTURES_ROOT / "funnels" / "example-vendor.funnel.json").read_text())
    return {
        "status": "ok",
        "funnel": funnel,
        "raw": {"note": "mock adapter — returns the existing hand-authored fixture; harness smoke-test only"},
    }
