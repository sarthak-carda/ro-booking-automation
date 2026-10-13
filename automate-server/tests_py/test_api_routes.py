"""Mirrors tests/api.test.js against the Python/FastAPI port.

Sets AUTOMATE_DATA_DIR to a throwaway temp dir *before* importing app.config
(so the module-level `config` singleton picks it up), same as the Node test
setting process.env.AUTOMATE_DATA_DIR before importing src/server.js.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_data_dir = tempfile.mkdtemp(prefix="automate-server-test-api-")
os.environ["AUTOMATE_DATA_DIR"] = _data_dir + "/"
os.environ["OPENAI_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import vendors, runs, self_heal_queue, script_registry  # noqa: E402
from tests_py.conftest import start_static_server  # noqa: E402

client = TestClient(app)

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures"
_TRIGGER_PORT = 4601


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_data_dir, ignore_errors=True)


def test_scripts_resolve_format_js_returns_raw_js():
    script_registry.set(
        "vendor.example.com",
        {
            "vendorId": 1,
            "portalUrl": "https://vendor.example.com/book",
            "source": "window.HeadoutAutomation = { run: async () => ({status:'PAYMENT_REQUIRED'}) };",
            "version": 1,
        },
    )
    res = client.get(
        "/api/scripts/resolve",
        params={"url": "https://vendor.example.com/book", "format": "js"},
    )
    assert res.status_code == 200
    assert "application/javascript" in res.headers["content-type"]
    assert res.text == "window.HeadoutAutomation = { run: async () => ({status:'PAYMENT_REQUIRED'}) };"


def test_scripts_resolve_404_for_unregistered_vendor():
    res = client.get(
        "/api/scripts/resolve",
        params={"url": "https://unknown-vendor.example.com", "format": "js"},
    )
    assert res.status_code == 404


def test_scripts_resolve_missing_url_400():
    res = client.get("/api/scripts/resolve")
    assert res.status_code == 400
    assert res.json() == {"error": "Missing required query param: url"}


def test_scripts_resolve_json_envelope_without_format():
    res = client.get("/api/scripts/resolve", params={"url": "https://vendor.example.com/book"})
    assert res.status_code == 200
    body = res.json()
    assert body["data"]["content"] == "window.HeadoutAutomation = { run: async () => ({status:'PAYMENT_REQUIRED'}) };"
    assert body["data"]["version"] == 1


def test_vendors_reports_health_per_vendor():
    vendors.set(
        2,
        {
            "id": 2,
            "name": "Test Vendor",
            "portalUrl": "https://x.example.com",
            "status": "active",
            "runHistory": [{"outcome": "shipped", "at": "2026-09-18T00:00:00Z"}],
        },
    )
    res = client.get("/api/vendors")
    data = res.json()["data"]
    v = next(x for x in data if x["id"] == 2)
    assert v["status"] == "active"
    assert v["successRate"] == 1


def test_vendor_not_found_404():
    res = client.get("/api/vendors/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"error": "Vendor not found"}


def test_runs_and_run_by_id():
    runs.set(
        "run-1",
        {"id": "run-1", "vendorId": 2, "status": "shipped", "steps": [], "createdAt": "2026-09-18T00:00:00Z"},
    )
    list_res = client.get("/api/runs", params={"vendorId": 2})
    assert len(list_res.json()["data"]) == 1

    single_res = client.get("/api/runs/run-1")
    assert single_res.json()["data"]["status"] == "shipped"


def test_run_not_found_404():
    res = client.get("/api/runs/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"error": "Run not found"}


def test_self_heal_queue_and_report():
    res = client.post(
        "/api/self-heal-queue/report",
        json={"vendorId": 2, "portalUrl": "https://x.example.com", "note": "RO says payment did not go through"},
    )
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["status"] == "escalated"
    assert data["trigger"] == "ro-report"

    list_res = client.get("/api/self-heal-queue", params={"status": "escalated"})
    assert any(item["id"] == data["id"] for item in list_res.json()["data"])


def test_self_heal_report_requires_vendor_and_portal():
    res = client.post("/api/self-heal-queue/report", json={})
    assert res.status_code == 400


def test_self_heal_item_not_found_404():
    res = client.get("/api/self-heal-queue/does-not-exist")
    assert res.status_code == 404
    assert res.json() == {"error": "Self-heal item not found"}


def test_eligible_vendors_reflects_threshold_job():
    res = client.get("/api/eligible-vendors")
    assert res.status_code == 200
    data = res.json()["data"]
    assert isinstance(data, list)
    assert len(data) > 0, "fixture data should produce at least one eligible vendor/tour"


def test_eligible_vendors_trigger_requires_vendor():
    res = client.post("/api/eligible-vendors/does-not-exist/trigger", json={})
    assert res.status_code == 404


def test_eligible_vendors_trigger_requires_booking_and_funnel():
    vendors.set(3, {"id": 3, "name": "Trigger Vendor", "portalUrl": "https://trigger.example.com"})
    res = client.post("/api/eligible-vendors/3/trigger", json={})
    assert res.status_code == 400


def test_eligible_vendors_trigger_success():
    """
    Regression note: this originally POSTed a dummy payload
    ({"sampleBooking": {"foo": "bar"}, "funnel": {"steps": []}}) against a
    non-resolvable https://trigger.example.com and expected 202. That only
    ever passed against an early no-op stub for run_vendor_pipeline -- the
    real pipeline correctly 400s/500s on that input (missing bookingId, an
    empty-steps funnel, and a portal URL Stage 3's Playwright audit can't
    reach). Fixed to exercise the real success path: a valid funnel/booking
    from the fixtures, audited against the local offline example-vendor page
    (same pattern as test_pipeline.py), so a 202 here actually means the
    trigger endpoint drove a real Stage 0-4 run to completion.
    """
    server = start_static_server(str(_FIXTURES_ROOT / "html" / "example-vendor"), _TRIGGER_PORT)
    try:
        vendor_fixture = json.loads((_FIXTURES_ROOT / "vendors.json").read_text())["9999"]
        vendor = {**vendor_fixture, "portalUrl": f"http://127.0.0.1:{_TRIGGER_PORT}/checkout.html"}
        vendors.set(vendor["id"], vendor)

        sample_booking = json.loads((_FIXTURES_ROOT / "bookings" / "sample-booking-1.json").read_text())
        funnel = json.loads((_FIXTURES_ROOT / "funnels" / "example-vendor.funnel.json").read_text())
        funnel = {**funnel, "portalUrl": vendor["portalUrl"]}

        res = client.post(
            f"/api/eligible-vendors/{vendor['id']}/trigger",
            json={"sampleBooking": sample_booking, "funnel": funnel},
        )
        assert res.status_code == 202, res.text
        body = res.json()
        assert "data" in body
        assert body["data"]["status"] == "shipped"
    finally:
        server.close()


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
