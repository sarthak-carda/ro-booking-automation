"""Verifies Stage 3 (audit gate) and the self-heal loop (drift-check, rewrite,
loop orchestration) against the offline example-vendor fixture, the same way
tests/pipeline.test.js's "audit gate fails loudly..." test and
tests/selfHeal.test.js's two tests do.

Serves fixtures/html/example-vendor over a real local HTTP server (Playwright
needs a real page navigation, not a file:// or in-memory DOM), using
http.server.ThreadingHTTPServer in a background thread instead of Node's
tests/helpers/staticServer.js.

The generated script under test comes from app.llm.mock.generate_script_from_funnel
— the already-landed Python port of src/openai/mock.js's deterministic
"convert" stand-in — rather than a hand-rolled duplicate, so this test
exercises the exact same `findEl(candidates, stepIndex, action)` contract the
rest of the pipeline produces.
"""
import functools
import http.server
import json
import re
import threading
from pathlib import Path
from typing import Iterator

import pytest

from app import config as config_module
from app import db as db_module
from app.db import self_heal_queue, vendors
from app.llm.mock import generate_script_from_funnel
from app.pipeline.audit import audit_script
from app.self_heal.loop import run_self_heal

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
HTML_ROOT = FIXTURES_ROOT / "html" / "example-vendor"

# Force every test in this module onto the deterministic offline mock, never
# a live OpenAI/Anthropic call — self-heal tests must be fast, free, and
# reproducible, not dependent on a live key being present in this dev
# environment's .env. Applied as a permanent, non-reverting assignment (not
# monkeypatch) deliberately: app.config's `config` object is a process-wide
# singleton created on first import (see app/config.py), so a *reverted*
# override would leave the singleton holding whatever real key .env supplies
# once this module's own tests finish — and if another test file in the same
# pytest invocation relies on `os.environ["OPENAI_API_KEY"] = ""` set before
# *its* import (which only forces the mock path if nothing else imported
# app.config first), that file would silently fall through to a real, billed
# API call instead. Confirmed this the hard way: running this file together
# with tests_py/test_self_heal.py in one pytest invocation produced a real
# GPT-5.6-sol completion (visible in the failing attempt's generated script,
# which used `poll()`/`readBookingValue`/`fillField` — names the deterministic
# mock never produces) once a monkeypatch-based version of this override
# reverted after this module's own tests completed.
config_module.config.openai.api_key = ""
config_module.config.anthropic_api_key = ""
config_module.config.llm_provider = "openai"


def _read_fixture(rel: str):
    return json.loads((FIXTURES_ROOT / rel).read_text(encoding="utf8"))


@pytest.fixture(scope="module")
def static_server() -> Iterator[int]:
    """Serves fixtures/html/example-vendor on a free localhost port, matching
    tests/helpers/staticServer.js's role for the Node test suite."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HTML_ROOT))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Points app.db's JSON-file-backed collections at a throwaway directory
    and drops the in-process cache, so vendor/self-heal-queue state from one
    test never leaks into the next — mirrors each Node test file's own
    mkdtempSync(...) + AUTOMATE_DATA_DIR, but per-test here for extra
    isolation since app.db's cache is process-global."""
    data_dir = str(tmp_path / "data") + "/"
    monkeypatch.setattr(config_module.config, "data_dir", data_dir)
    db_module._cache.clear()
    yield
    db_module._cache.clear()


@pytest.mark.asyncio
async def test_audit_gate_fails_loudly_when_vendor_page_is_missing_an_expected_field(static_server):
    """Ports tests/pipeline.test.js's "audit gate fails loudly when the vendor
    page is missing an expected field"."""
    funnel = _read_fixture("funnels/example-vendor.funnel.json")
    booking = _read_fixture("bookings/sample-booking-1.json")
    script = generate_script_from_funnel(funnel)
    broken_url = f"http://127.0.0.1:{static_server}/checkout-broken.html"

    result = await audit_script(script=script, booking=booking, portal_url=broken_url)

    assert result["pass"] is False
    assert result["error"] is not None
    assert re.search(r"Step 0 \(fill\): no element matched candidates", result["error"])


@pytest.mark.asyncio
async def test_audit_gate_passes_against_the_unmodified_checkout_page(static_server):
    """Sanity check that the audit gate actually passes on the non-drifted
    fixture (so the failure above is proven to be about the drift, not a
    broken generator/gate)."""
    funnel = _read_fixture("funnels/example-vendor.funnel.json")
    booking = _read_fixture("bookings/sample-booking-1.json")
    script = generate_script_from_funnel(funnel)
    good_url = f"http://127.0.0.1:{static_server}/checkout.html"

    result = await audit_script(script=script, booking=booking, portal_url=good_url)

    assert result["pass"] is True, result
    assert result["status"] == "PAYMENT_REQUIRED"


@pytest.mark.asyncio
async def test_self_heal_detects_drift_patches_the_script_and_resolves(static_server):
    """Ports tests/selfHeal.test.js's "self-heal loop detects a
    deliberately-drifted vendor field, patches the script, and resolves"."""
    vendor_id = 9999
    good_url = f"http://127.0.0.1:{static_server}/checkout.html"
    broken_url = f"http://127.0.0.1:{static_server}/checkout-broken.html"
    vendors.set(vendor_id, {"id": vendor_id, "name": "Example Vendor", "portalUrl": good_url})

    funnel = _read_fixture("funnels/example-vendor.funnel.json")
    booking = _read_fixture("bookings/sample-booking-1.json")
    script = generate_script_from_funnel(funnel)

    shipped_audit = await audit_script(script=script, booking=booking, portal_url=good_url)
    assert shipped_audit["pass"] is True, shipped_audit

    # Simulate the vendor changing their page (id="first-name" -> id="given-name")
    # by re-auditing the SAME shipped script against the drifted fixture page.
    drift = await audit_script(script=script, booking=booking, portal_url=broken_url)
    assert drift["pass"] is False
    assert re.search(r"Step 0 \(fill\): no element matched candidates", drift["error"])

    reference_dom = (HTML_ROOT / "checkout.html").read_text(encoding="utf8")
    current_dom = (HTML_ROOT / "checkout-broken.html").read_text(encoding="utf8")

    healed = await run_self_heal(
        vendor_id=vendor_id,
        portal_url=broken_url,
        script=script,
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
    assert vendors.get(vendor_id)["status"] == "active"


@pytest.mark.asyncio
async def test_self_heal_escalates_after_exhausting_retries_when_no_fix_is_found(static_server):
    """Ports tests/selfHeal.test.js's "self-heal escalates after exhausting
    retries when no fix is found" — an unstructured failure error isn't
    selector-shaped, so this should escalate immediately without attempting
    any diagnosis."""
    vendor_id = 12345
    vendors.set(vendor_id, {"id": vendor_id, "name": "Unfixable Vendor"})

    healed = await run_self_heal(
        vendor_id=vendor_id,
        portal_url=f"http://127.0.0.1:{static_server}/checkout.html",
        script="window.HeadoutAutomation = { run: () => ({status:'ERROR'}) };",
        failure_error="Totally unstructured failure, no step info",
        booking={},
        reference_dom="<html></html>",
        current_dom="<html></html>",
        cap=2,
    )

    assert healed["status"] == "escalated"
    assert re.search(r"cannot auto-diagnose", healed["escalationReason"])
    assert vendors.get(vendor_id)["status"] == "needs_review"
