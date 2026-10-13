"""Ports tests/selfHeal.test.js.

Depends on `app.pipeline.run_pipeline.run_vendor_pipeline`,
`app.pipeline.audit.audit_script` (Stage 0/2/4 pipeline + Stage 3 audit,
other agents' slices) and `app.self_heal.loop.run_self_heal` (Stage 3
self-heal, another agent's slice). As of this pass none of `app/pipeline/`
or `app/self_heal/` exist yet -- expected per the parallel-build split, see
the orchestrator report.

Run standalone: `.venv/bin/pytest tests_py/test_self_heal.py -v` (see the
process-isolation note in test_threshold.py -- same caveat applies here).
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

PORT = 4600
_DATA_DIR = tempfile.mkdtemp(prefix="automate-server-test-selfheal-")
os.environ["AUTOMATE_DATA_DIR"] = f"{_DATA_DIR}/"
os.environ["OPENAI_API_KEY"] = ""
os.environ["KEEPER_STUB"] = "true"
os.environ["ZENDESK_STUB"] = "true"

from app.db import vendors, self_heal_queue  # noqa: E402
from app.pipeline.run_pipeline import run_vendor_pipeline  # noqa: E402
from app.pipeline.audit import audit_script  # noqa: E402
from app.self_heal.loop import run_self_heal  # noqa: E402

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
async def test_self_heal_detects_drift_patches_script_and_resolves():
    """Self-heal loop detects a deliberately-drifted vendor field, patches the script, and resolves."""
    good_vendor = {**read_fixture("vendors.json")["9999"], "portalUrl": f"http://127.0.0.1:{PORT}/checkout.html"}
    booking = read_fixture("bookings/sample-booking-1.json")
    funnel = read_fixture("funnels/example-vendor.funnel.json")
    vendors.set(good_vendor["id"], good_vendor)

    shipped = await run_vendor_pipeline(vendor=good_vendor, sample_booking=booking, funnel=funnel)
    assert shipped["status"] == "shipped"

    # Simulate the vendor changing their page (id="first-name" -> id="given-name")
    # by re-auditing the SAME shipped script against the drifted fixture page.
    drift = await audit_script(
        script=shipped["script"],
        booking=booking,
        portal_url=f"http://127.0.0.1:{PORT}/checkout-broken.html",
    )
    assert drift["pass"] is False
    assert "Step 0 (fill): no element matched candidates" in drift["error"]

    reference_dom = (FIXTURES_ROOT / "html/example-vendor/checkout.html").read_text(encoding="utf8")
    current_dom = (FIXTURES_ROOT / "html/example-vendor/checkout-broken.html").read_text(encoding="utf8")

    healed = await run_self_heal(
        vendor_id=good_vendor["id"],
        portal_url=f"http://127.0.0.1:{PORT}/checkout-broken.html",
        script=shipped["script"],
        failure_error=drift["error"],
        booking=booking,
        reference_dom=reference_dom,
        current_dom=current_dom,
    )

    assert healed["status"] == "resolved", json.dumps(healed, indent=2, default=str)
    assert len(healed["attempts"]) == 1, "should resolve on the first attempt for a single-field drift"
    assert re.search(r"Field relocated.*First name", healed["attempts"][0]["diagnosis"]["diagnosis"])

    queued = self_heal_queue.get(healed["id"])
    assert queued["status"] == "resolved"
    assert vendors.get(good_vendor["id"])["status"] == "active"


@pytest.mark.asyncio
async def test_self_heal_escalates_after_exhausting_retries():
    """Self-heal escalates after exhausting retries when no fix is found."""
    vendor_id = 12345
    vendors.set(vendor_id, {"id": vendor_id, "name": "Unfixable Vendor"})

    healed = await run_self_heal(
        vendor_id=vendor_id,
        portal_url=f"http://127.0.0.1:{PORT}/checkout.html",
        script="window.HeadoutAutomation = { run: () => ({status:'ERROR'}) };",
        failure_error="Totally unstructured failure, no step info",
        booking={},
        reference_dom="<html></html>",
        current_dom="<html></html>",
        cap=2,
    )

    assert healed["status"] == "escalated"
    assert "cannot auto-diagnose" in healed["escalationReason"]
    assert vendors.get(vendor_id)["status"] == "needs_review"
