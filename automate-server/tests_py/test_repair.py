"""Unit tests for app.pipeline.repair (Stage 2.5 — automated self-repair loop).

Tests repair_script()'s own control flow (environment-blocked cadence, attempt
cap, narrow-vs-broad fallback policy) and run_pipeline.py's second-booking
cap-sharing wiring, all via mocking at the module boundary (audit_script,
repair_funnel_script, diagnose_drift, rewrite_script) — deterministic and
fast, no real browser or HTTP server needed. End-to-end (real Playwright,
real HTTP fixture) coverage lives in tests_py/test_pipeline.py instead,
exercising this module as a dependency rather than re-proving its internals.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.repair import DEFAULT_REPAIR_CAP, detect_environment_blocked, repair_script

GENERATION_REQUEST = {
    "vendorName": "Example",
    "portalUrl": "http://example.test",
    "requiresCredentials": False,
    "bookingSchemaSample": {},
}
FUNNEL = {"steps": [{"index": 0, "action": "fill", "selector_candidates": ["#x"], "dom_snapshot": "<html></html>"}]}
BOOKING = {"bookingId": 1}
SELECTOR_ERROR = 'Step 0 (fill): no element matched candidates ["#x"]'


def _audit(pass_=False, error=SELECTOR_ERROR, dom="<html></html>", status="ERROR"):
    return {"pass": pass_, "status": status, "error": error, "dom": dom, "screenshots": []}


class TestDetectEnvironmentBlocked:
    def test_no_match_returns_none(self):
        assert detect_environment_blocked(error=SELECTOR_ERROR, dom="<html></html>") is None

    def test_cloudflare_marker_in_dom_not_just_error(self):
        # Real finding this module is built around: the error text alone is identical for
        # real drift and a bot-check page -- only the DOM distinguishes them.
        assert detect_environment_blocked(error=SELECTOR_ERROR, dom="<div id='cf-turnstile-response'></div>") == "cloudflare_challenge"

    def test_captcha_marker(self):
        assert detect_environment_blocked(error=None, dom="<div data-sitekey='x'></div>") == "captcha_challenge"

    def test_sold_out_in_error(self):
        assert detect_environment_blocked(error="No availability for this date", dom=None) == "inventory_unavailable"

    def test_rate_limit_in_error(self):
        assert detect_environment_blocked(error="429 Too Many Requests", dom=None) == "rate_limited"


@pytest.mark.asyncio
async def test_blocked_on_initial_audit_spends_zero_repair_attempts():
    blocked_initial = _audit(dom="<div class='g-recaptcha'></div>")

    with patch("app.pipeline.repair.audit_script", new=AsyncMock()) as mock_audit:
        result = await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=blocked_initial, cap=3,
        )

    assert result["outcome"] == "environment_blocked"
    mock_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cadence_blocked_appearing_only_on_a_later_attempt_stops_immediately():
    """The whole reason this check exists is 'repeated automated hits risk anti-bot
    flags' -- a block that only appears starting on a LATER attempt (the most plausible
    trigger for exactly that) must stop the loop right there, not be spent treating it
    as ordinary drift for the rest of the cap."""
    initial_audit = _audit(dom="<html>normal page</html>")
    blocked_audit = _audit(dom="<div id='cf-turnstile-response'></div>")

    with patch("app.pipeline.repair.audit_script", new=AsyncMock(return_value=blocked_audit)) as mock_audit, \
         patch("app.pipeline.repair.diagnose_drift", new=AsyncMock(side_effect=ValueError("no diagnosis"))), \
         patch("app.pipeline.repair.repair_funnel_script", new=AsyncMock(return_value={"script": "patched", "mocked": True})), \
         patch("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0):
        result = await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=initial_audit, cap=3,
        )

    assert result["outcome"] == "environment_blocked"
    assert mock_audit.await_count == 1  # stopped after exactly ONE repair audit, not all 3
    assert result["attempts"][-1]["outcome"] == "environment_blocked"


@pytest.mark.asyncio
async def test_cap_exhaustion_stops_at_exactly_cap_real_audits():
    always_failing = _audit(dom="<html>never blocked, never fixed</html>")

    with patch("app.pipeline.repair.audit_script", new=AsyncMock(return_value=always_failing)) as mock_audit, \
         patch("app.pipeline.repair.diagnose_drift", new=AsyncMock(side_effect=ValueError("no diagnosis"))), \
         patch("app.pipeline.repair.repair_funnel_script", new=AsyncMock(return_value={"script": "patched", "mocked": True})), \
         patch("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0):
        result = await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=always_failing, cap=3,
        )

    assert result["outcome"] == "exhausted"
    assert mock_audit.await_count == 3
    assert len(result["attempts"]) == 3
    assert all(a["outcome"] == "failed" for a in result["attempts"])


@pytest.mark.asyncio
async def test_narrow_patch_tried_at_most_once_then_falls_back_to_broad_for_the_rest():
    always_failing = _audit()

    with patch("app.pipeline.repair.audit_script", new=AsyncMock(return_value=always_failing)), \
         patch("app.pipeline.repair.diagnose_drift", new=AsyncMock(side_effect=ValueError("no diagnosis"))) as mock_diagnose, \
         patch("app.pipeline.repair.repair_funnel_script", new=AsyncMock(return_value={"script": "patched", "mocked": True})) as mock_broad, \
         patch("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0):
        result = await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=always_failing, cap=3,
        )

    assert mock_diagnose.await_count == 1  # tried once, never retried after it couldn't help
    assert mock_broad.await_count == 3     # used for every attempt once narrow gave up
    assert result["outcome"] == "exhausted"


@pytest.mark.asyncio
async def test_narrow_patch_resolving_never_needs_broad_repair():
    failing_audit = _audit()
    passing_audit = {"pass": True, "status": "PAYMENT_REQUIRED", "error": None, "dom": None, "screenshots": []}

    with patch("app.pipeline.repair.audit_script", new=AsyncMock(return_value=passing_audit)), \
         patch(
             "app.pipeline.repair.diagnose_drift",
             new=AsyncMock(return_value={"step_index": 0, "diagnosis": "moved", "suggested_selectors": ["#given-name"]}),
         ), \
         patch("app.pipeline.repair.rewrite_script", return_value={"script": "patched", "mocked": False}), \
         patch("app.pipeline.repair.repair_funnel_script", new=AsyncMock()) as mock_broad, \
         patch("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0):
        result = await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=failing_audit, cap=3,
        )

    assert result["outcome"] == "resolved"
    assert result["attempts"][0]["mode"] == "narrow"
    mock_broad.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_attempt_tags_every_record_with_the_given_booking_label():
    always_failing = _audit()
    seen = []

    with patch("app.pipeline.repair.audit_script", new=AsyncMock(return_value=always_failing)), \
         patch("app.pipeline.repair.diagnose_drift", new=AsyncMock(side_effect=ValueError("no diagnosis"))), \
         patch("app.pipeline.repair.repair_funnel_script", new=AsyncMock(return_value={"script": "patched", "mocked": True})), \
         patch("app.pipeline.repair.REPAIR_BACKOFF_SECONDS", 0):
        await repair_script(
            generation_request=GENERATION_REQUEST, funnel=FUNNEL, script="original", booking=BOOKING,
            portal_url="http://example.test", initial_audit=always_failing, cap=1,
            on_attempt=seen.append, booking_label="overfit",
        )

    assert seen and all(record["booking"] == "overfit" for record in seen)


@pytest.mark.asyncio
async def test_run_pipeline_guarantees_second_booking_at_least_one_repair_attempt(monkeypatch, tmp_path):
    """Second-booking (overfit-check) repair shares the run's overall attempt budget
    with the first booking's repair rather than getting a fresh cap of its own -- but
    a STRICT shared cap could fully starve it (first booking's repair could in
    principle use the whole budget). It must always get at least one attempt."""
    from app import config as config_module
    from app import db as db_module
    import app.pipeline.run_pipeline as run_pipeline_module

    monkeypatch.setattr(config_module.config, "data_dir", str(tmp_path / "data") + "/")
    db_module._cache.clear()

    vendor = {"id": "v-repair-test", "name": "Example", "portalUrl": "http://example.test", "requiresCredentials": False}
    booking1 = {"bookingId": 1, "guestDetails": {}}
    booking2 = {"bookingId": 2, "guestDetails": {}}
    funnel = {"steps": [{"index": 0, "action": "click", "selector_candidates": ["#x"]}]}

    async def fake_audit_script(*, script, booking, credentials=None, portal_url):
        return {"pass": False, "status": "ERROR", "error": "boom", "dom": None, "screenshots": []}

    async def fake_convert(generation_request, funnel_):
        return {"script": "window.HeadoutAutomation = {};", "mocked": True}

    repair_calls = []

    async def fake_repair_script(*, generation_request, funnel, script, booking, portal_url,
                                  credentials=None, initial_audit, cap, on_attempt=None, booking_label="primary"):
        repair_calls.append({"booking_label": booking_label, "cap": cap})
        # Primary booking "resolves", but only after using up the ENTIRE cap it was given --
        # the scenario this guarantee exists for.
        attempts_used = cap if booking_label == "primary" else 1
        return {
            "outcome": "resolved",
            "script": script,
            "audit": {"pass": True, "status": "PAYMENT_REQUIRED", "error": None},
            "attempts": [{"attempt": i} for i in range(1, attempts_used + 1)],
        }

    monkeypatch.setattr(run_pipeline_module, "audit_script", fake_audit_script)
    monkeypatch.setattr(run_pipeline_module, "convert_funnel_to_script", fake_convert)
    monkeypatch.setattr(run_pipeline_module, "repair_script", fake_repair_script)

    result = await run_pipeline_module.run_vendor_pipeline(
        vendor=vendor, sample_booking=booking1, second_sample_booking=booking2, funnel=funnel,
    )

    assert len(repair_calls) == 2
    assert repair_calls[0] == {"booking_label": "primary", "cap": DEFAULT_REPAIR_CAP}
    # Primary used the full shared budget (DEFAULT_REPAIR_CAP attempts) -- a naive
    # `DEFAULT_REPAIR_CAP - attempts_used` would give the second booking cap=0. It must
    # still get 1, never 0.
    assert repair_calls[1] == {"booking_label": "overfit", "cap": 1}
    assert result["status"] == "shipped"
