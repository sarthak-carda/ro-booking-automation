"""Mirrors src/routes/vendors.js.

GET /api/vendors -- health per vendor: active/failing, last N run outcomes, success rate.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import vendors

router = APIRouter()


def _summarize(vendor: dict, include_history: bool = False) -> dict:
    history = vendor.get("runHistory") or []
    success_count = sum(1 for h in history if h.get("outcome") in ("shipped", "resolved"))
    result = {
        "id": vendor.get("id"),
        "name": vendor.get("name"),
        "portalUrl": vendor.get("portalUrl"),
        "requiresCredentials": bool(vendor.get("requiresCredentials")),
        "status": vendor.get("status") or "unknown",
        "successRate": (success_count / len(history)) if history else None,
        "lastNOutcomes": history[-10:],
    }
    if include_history:
        result["runHistory"] = history
    return result


@router.get("")
async def list_vendors():
    return JSONResponse(content={"data": [_summarize(v) for v in vendors.list()]})


@router.get("/{vendor_id}")
async def get_vendor(vendor_id: str):
    vendor = vendors.get(vendor_id)
    if not vendor:
        return JSONResponse(status_code=404, content={"error": "Vendor not found"})
    return JSONResponse(content={"data": _summarize(vendor, include_history=True)})
