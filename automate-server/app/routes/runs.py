"""Mirrors src/routes/runs.js.

GET /api/runs, GET /api/runs/:id -- run history/status, queued -> capturing ->
converting -> auditing -> shipped/failed.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import runs

router = APIRouter()


@router.get("")
async def list_runs(request: Request):
    vendor_id = request.query_params.get("vendorId")
    run_list = sorted(runs.list(), key=lambda r: r.get("createdAt") or "", reverse=True)
    if vendor_id is not None:
        run_list = [r for r in run_list if str(r.get("vendorId")) == str(vendor_id)]
    return JSONResponse(content={"data": run_list})


@router.get("/{run_id}")
async def get_run(run_id: str):
    run = runs.get(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    return JSONResponse(content={"data": run})
