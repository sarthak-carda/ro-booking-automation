"""Mirrors src/routes/selfHealQueue.js.

GET /api/self-heal-queue -- items that exhausted retries (or came from an RO
report) and need manual triage, each with a reference vs. current
screenshot/DOM pair. POST /report lets the extension's payment-confirm UI
surface an RO person's "No" (trigger (b) in the plan) instead of it being
assumed.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import self_heal_queue

router = APIRouter()

try:
    from app.self_heal.loop import trigger_from_ro_report  # type: ignore
except ImportError:
    # TODO: replace with real import once app.self_heal.loop lands (another agent's slice).
    # Local stub matching src/selfHeal/loop.js's triggerFromRoReport, so routing/response-shape
    # logic can be verified independently until the real module exists.
    import uuid
    from datetime import datetime, timezone

    from app.db import vendors as _vendors

    def trigger_from_ro_report(
        *,
        vendor_id,
        portal_url,
        run_id=None,
        reference_screenshot=None,
        failure_screenshot=None,
        note=None,
    ):
        item_id = str(uuid.uuid4())
        self_heal_queue.set(
            item_id,
            {
                "vendorId": vendor_id,
                "portalUrl": portal_url,
                "runId": run_id,
                "trigger": "ro-report",
                "status": "escalated",
                "escalationReason": note
                or "RO reported payment did not go through; no structural signal to auto-diagnose",
                "referenceScreenshot": reference_screenshot,
                "failureScreenshot": failure_screenshot,
                "attempts": [],
                "cap": 0,
                "startedAt": datetime.now(timezone.utc).isoformat(),
            },
        )
        vendor = _vendors.get(vendor_id)
        if vendor:
            _vendors.upsert(vendor_id, {"status": "needs_review"})
        return self_heal_queue.get(item_id)


@router.get("")
async def list_self_heal(request: Request):
    status = request.query_params.get("status")
    items = sorted(self_heal_queue.list(), key=lambda i: i.get("startedAt") or "", reverse=True)
    if status:
        items = [i for i in items if i.get("status") == status]
    return JSONResponse(content={"data": items})


@router.get("/{item_id}")
async def get_self_heal(item_id: str):
    item = self_heal_queue.get(item_id)
    if not item:
        return JSONResponse(status_code=404, content={"error": "Self-heal item not found"})
    return JSONResponse(content={"data": item})


@router.post("/report")
async def report(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    vendor_id = body.get("vendorId")
    portal_url = body.get("portalUrl")
    run_id = body.get("runId")
    reference_screenshot = body.get("referenceScreenshot")
    failure_screenshot = body.get("failureScreenshot")
    note = body.get("note")

    if not vendor_id or not portal_url:
        return JSONResponse(status_code=400, content={"error": "vendorId and portalUrl are required"})

    item = trigger_from_ro_report(
        vendor_id=vendor_id,
        portal_url=portal_url,
        run_id=run_id,
        reference_screenshot=reference_screenshot,
        failure_screenshot=failure_screenshot,
        note=note,
    )
    return JSONResponse(status_code=201, content={"data": item})
