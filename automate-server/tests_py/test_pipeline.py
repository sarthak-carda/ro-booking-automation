"""Ports tests/pipeline.test.js.

Depends on `app.pipeline.run_pipeline.run_vendor_pipeline` (Stage 0/2/4
pipeline, another agent's slice -- as of this pass, `app/pipeline/` does not
exist yet) and `app.main.create_app` (this agent's slice already provides
that). If `app.pipeline.run_pipeline` is not importable yet, that's expected
per the parallel-build split -- see the orchestrator report.

Run standalone: `.venv/bin/pytest tests_py/test_pipeline.py -v` (see the
process-isolation note in test_threshold.py -- same caveat applies here).
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
import httpx

PORT = 4599
_DATA_DIR = tempfile.mkdtemp(prefix="automate-server-test-pipeline-")
os.environ["AUTOMATE_DATA_DIR"] = f"{_DATA_DIR}/"
os.environ["OPENAI_API_KEY"] = ""  # force the offline mock generator
os.environ["KEEPER_STUB"] = "true"
os.environ["ZENDESK_STUB"] = "true"

from app.db import vendors, runs, script_registry  # noqa: E402
from app.pipeline.run_pipeline import run_vendor_pipeline  # noqa: E402
from app.main import create_app  # noqa: E402

from tests_py.conftest import start_static_server  # noqa: E402

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"


def read_fixture(rel: str):
    with open(FIXTURES_ROOT / rel, "r", encoding="utf8") as f:
        return json.load(f)


@pytest.fixture(scope="module", autouse=True)
def _static_server_and_cleanup():
    server = start_static_server(str(FIXTURES_ROOT / "html" / "example-vendor"), PORT)
    yield server
    server.close()
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.mark.asyncio
async def test_pipeline_ships_working_script_and_resolves_via_http_route():
    """Stage 0-4 pipeline ships a working script and it resolves via the real HTTP route."""
    vendor = read_fixture("vendors.json")["9999"]
    vendors.set(vendor["id"], vendor)
    booking1 = read_fixture("bookings/sample-booking-1.json")
    booking2 = read_fixture("bookings/sample-booking-2.json")
    funnel = read_fixture("funnels/example-vendor.funnel.json")

    result = await run_vendor_pipeline(
        vendor=vendor,
        sample_booking=booking1,
        second_sample_booking=booking2,
        funnel=funnel,
    )

    assert result["status"] == "shipped"
    assert result["audit"]["pass"] is True, f"First-booking audit should pass: {result['audit'].get('error')}"
    assert result["secondAudit"]["pass"] is True, (
        f"Second-booking (overfit check) should pass: {result['secondAudit'].get('error')}"
    )
    assert "window.HeadoutAutomation" in result["script"]
    assert "demo-not-a-real-password" not in result["script"]  # never bakes credentials in

    registry_entry = script_registry.get("127.0.0.1:4599")
    assert registry_entry, "script should be registered under the vendor host"

    run = runs.get(result["runId"])
    assert run["status"] == "shipped"
    assert [s["name"] for s in run["steps"]] == [
        "capture",
        "convert",
        "audit:primary-booking",
        "audit:second-booking",
        "ship",
    ]

    # Resolve exactly the way the extension's fetchAutofillScript does.
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        res = await ac.get(
            "/api/scripts/resolve",
            params={"url": vendor["portalUrl"], "format": "js"},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].split(";")[0] == "application/javascript"
        assert "HeadoutAutomation" in res.text


@pytest.mark.asyncio
async def test_repair_loop_resolves_a_relocated_field_and_ships():
    """Stage 2.5 (app/pipeline/repair.py) end to end: the first audit fails against
    checkout-broken.html (the first-name field's id+name both changed, label text
    unchanged -- the self-heal drift-check fixture, see its own file comment), the
    repair loop's narrow self-heal patch relocates it via the stable label text, the
    re-audit passes, and the run ships -- all without a human. This used to be the
    "audit gate fails loudly" test, back when the pipeline had no repair capability at
    all; now that it does, this exact fixture is the intended repair-resolves case."""
    vendor = {**read_fixture("vendors.json")["9999"], "portalUrl": f"http://127.0.0.1:{PORT}/checkout-broken.html"}
    booking1 = read_fixture("bookings/sample-booking-1.json")
    funnel = read_fixture("funnels/example-vendor.funnel.json")

    result = await run_vendor_pipeline(vendor=vendor, sample_booking=booking1, funnel=funnel, auto_ship=True)

    assert result["status"] == "shipped", f"Expected repair to resolve and ship: {result.get('reason')}"
    assert result["audit"]["pass"] is True

    run = runs.get(result["runId"])
    repair_steps = [s for s in run["steps"] if s["name"].startswith("repair")]
    assert repair_steps, "expected at least one repair attempt to be recorded on the run"


@pytest.mark.asyncio
async def test_repair_loop_fails_loudly_when_genuinely_unrepairable():
    """The audit gate's core guarantee -- never silently ship a broken script -- must
    still hold when a failure genuinely can't be fixed. checkout-unrepairable.html
    removes the first-name field entirely (no id/name/label survives anywhere), so
    neither the narrow self-heal patch nor the offline mock's broad repair (which just
    regenerates from the same funnel, blind to page content) can ever produce a script
    that passes against it -- the loop must exhaust its cap and fail, not ship."""
    vendor = {**read_fixture("vendors.json")["9999"], "portalUrl": f"http://127.0.0.1:{PORT}/checkout-unrepairable.html"}
    booking1 = read_fixture("bookings/sample-booking-1.json")
    funnel = read_fixture("funnels/example-vendor.funnel.json")

    result = await run_vendor_pipeline(vendor=vendor, sample_booking=booking1, funnel=funnel, auto_ship=True)

    assert result["status"] == "failed"
    assert "Step 0 (fill): no element matched candidates" in result["reason"]

    run = runs.get(result["runId"])
    repair_steps = [s for s in run["steps"] if s["name"].startswith("repair:attempt-")]
    from app.pipeline.repair import DEFAULT_REPAIR_CAP
    assert len(repair_steps) == DEFAULT_REPAIR_CAP, "should exhaust the full repair cap before giving up"
