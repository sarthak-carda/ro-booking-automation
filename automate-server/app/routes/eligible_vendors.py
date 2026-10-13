"""Mirrors src/routes/eligibleVendors.js.

GET /api/eligible-vendors -- vendors/tours approaching or past the threshold.
POST /api/eligible-vendors/:vendorId/trigger -- manual "trigger now" action,
feeding straight into the same Stage 0-4 pipeline (not a separate path).
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import vendors

router = APIRouter()

try:
    from app.threshold.check_threshold import check_eligible_vendors  # type: ignore
except ImportError:
    # TODO: replace with real import once app.threshold.check_threshold lands (another agent's slice).
    # Mirrors src/threshold/checkThreshold.js + src/zendesk/zendeskFixture.js closely enough to
    # exercise real routing/response-shape logic against the actual fixture data.
    import json

    from app.config import config
    from app.db import script_registry
    from app.lib.vendor_url import host_for

    async def check_eligible_vendors(vendor_portal_by_id=None):
        vendor_portal_by_id = vendor_portal_by_id or {}
        if not config.zendesk_stub:
            raise RuntimeError(
                "Real Zendesk integration is not wired up. Set ZENDESK_STUB=true or implement a real pull here."
            )
        fixture_path = f"{config.fixtures_dir}zendesk-tickets.fixture.json"
        with open(fixture_path, "r", encoding="utf8") as f:
            rows = json.load(f)

        threshold = config.automation_threshold
        eligible = []
        for row in rows:
            if row.get("manualTicketCount", 0) < threshold:
                continue
            portal = vendor_portal_by_id.get(row.get("vendorId"))
            portal_url = portal.get("portalUrl") if portal else None
            already_shipped = bool(script_registry.get(host_for(portal_url))) if portal_url else False
            eligible.append({**row, "threshold": threshold, "alreadyShipped": already_shipped})
        return eligible


try:
    from app.pipeline.run_pipeline import run_vendor_pipeline  # type: ignore
except ImportError:
    # TODO: replace with real import once app.pipeline.run_pipeline lands (another agent's slice).
    import uuid
    from datetime import datetime, timezone

    from app.db import runs as _runs

    async def run_vendor_pipeline(
        *,
        vendor,
        sample_booking,
        second_sample_booking=None,
        funnel,
        credentials=None,
        auto_ship=True,
        reviewed_by=None,
    ):
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        return _runs.upsert(
            run_id,
            {
                "kind": "pipeline",
                "vendorId": vendor.get("id"),
                "status": "queued",
                "steps": [],
                "createdAt": now,
                "updatedAt": now,
            },
        )


@router.get("")
async def list_eligible_vendors():
    try:
        portal_by_id = {v.get("id"): v for v in vendors.list()}
        eligible = await check_eligible_vendors(portal_by_id)
        return JSONResponse(content={"data": eligible})
    except Exception as err:  # noqa: BLE001 - mirrors Node's catch (err) => 500
        return JSONResponse(status_code=500, content={"error": str(err)})


@router.post("/{vendor_id}/trigger")
async def trigger_vendor(vendor_id: str, request: Request):
    vendor = vendors.get(vendor_id)
    if not vendor:
        return JSONResponse(status_code=404, content={"error": "Vendor not found; register it first"})

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    sample_booking = body.get("sampleBooking")
    second_sample_booking = body.get("secondSampleBooking")
    funnel = body.get("funnel")
    credentials = body.get("credentials")
    auto_ship = body.get("autoShip")

    if not sample_booking or not funnel:
        return JSONResponse(
            status_code=400,
            content={"error": "sampleBooking and funnel are required to trigger the pipeline"},
        )

    try:
        result = await run_vendor_pipeline(
            vendor=vendor,
            sample_booking=sample_booking,
            second_sample_booking=second_sample_booking,
            funnel=funnel,
            credentials=credentials,
            auto_ship=auto_ship if auto_ship is not None else True,
        )
        return JSONResponse(status_code=202, content={"data": result})
    except Exception as err:  # noqa: BLE001 - mirrors Node's catch (err) => 500
        return JSONResponse(status_code=500, content={"error": str(err)})
