"""Mirrors src/routes/scripts.js.

GET /api/scripts/resolve?url=<vendorLink>&format=js

Matches the extension's `fetchAutofillScript` (lib/autofill.js in the
extension repo): it does `res.text()`, not `res.json()`, when `format=js` is
passed -- so this route must return raw JS with that param, and can return a
JSON envelope otherwise (kept for tooling/debugging).
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.db import script_registry
from app.lib.vendor_url import host_for

router = APIRouter()


@router.get("/resolve")
async def resolve(request: Request):
    url = request.query_params.get("url")
    fmt = request.query_params.get("format")

    if not url:
        return JSONResponse(status_code=400, content={"error": "Missing required query param: url"})

    host = host_for(str(url))
    entry = script_registry.get(host)

    if not entry:
        if fmt == "js":
            return Response(
                status_code=404,
                media_type="text/plain",
                content=f"// No shipped script for host {host}",
            )
        return JSONResponse(status_code=404, content={"error": f"No shipped script for host {host}"})

    if fmt == "js":
        return Response(status_code=200, media_type="application/javascript", content=entry["source"])

    return JSONResponse(
        content={
            "data": {
                "content": entry.get("source"),
                "version": entry.get("version"),
                "shippedAt": entry.get("shippedAt"),
            }
        }
    )
