"""
Scores one adapter's capture result by running it through the SAME Stage 2
(convert) + Stage 3 (audit) pipeline the real service uses — an objective
pass/fail instead of a subjective read of the tool's output. See
bakeoff/README.md for the full comparison methodology.

Port of bakeoff/lib/scorecard.js. The three Stage 2/3 functions below are
another agent's slice of this same port (app/pipeline/capture.py,
convert.py, audit.py) — imported here, never reimplemented. If they aren't
importable yet, `score_capture_result` still runs (capture + validation
status still gets reported), it just can't score past validation and says so
in the row's notes instead of raising. Signature guess for the not-yet-built
functions mirrors the JS API 1:1 in snake_case, matching this repo's
established porting convention elsewhere (see app/config.py):
  validate_funnel_recording(funnel: dict) -> None  (raises on schema mismatch)
  await convert_funnel_to_script(generation_request=..., funnel=...) -> {"script": str, "mocked": bool}
  await audit_script(script=..., booking=..., credentials=..., portal_url=...) -> {"pass": bool, "status": str, "error": str|None, "screenshots": list}
If the real signatures differ once that slice lands, this is the one file to
reconcile.
"""
from typing import Any

try:
    from app.pipeline.capture import validate_funnel_recording
    from app.pipeline.convert import convert_funnel_to_script
    from app.pipeline.audit import audit_script

    PIPELINE_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised until the other agent's slice lands
    validate_funnel_recording = None  # type: ignore[assignment]
    convert_funnel_to_script = None  # type: ignore[assignment]
    audit_script = None  # type: ignore[assignment]
    PIPELINE_IMPORT_ERROR = str(exc)


async def score_capture_result(
    *,
    tool: str,
    adapter_result: dict,
    generation_request: dict,
    booking: dict,
    second_booking: dict | None = None,
    portal_url: str,
    credentials: dict | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tool": tool,
        "captureStatus": adapter_result.get("status"),
        "captureDurationMs": adapter_result.get("durationMs"),
        "notes": [],
    }

    if adapter_result.get("status") == "not_configured":
        row["notes"].append(adapter_result.get("reason") or "not configured")
        return row
    if adapter_result.get("status") == "error":
        row["notes"].append(adapter_result.get("error") or "capture failed")
        return row

    if PIPELINE_IMPORT_ERROR:
        row["notes"].append(
            "Stage 2/3 pipeline (app.pipeline.capture/convert/audit) not importable yet — "
            f"another agent's slice, blocked: {PIPELINE_IMPORT_ERROR}. Capture succeeded but "
            "could not be scored past this point."
        )
        return row

    funnel = adapter_result.get("funnel")
    row["funnel"] = funnel  # kept so a captured funnel can be re-scored under a different
    # LLM_PROVIDER later without a second, real, live-site capture — capture (Stage 1) is
    # independent of which LLM does Stage 2 conversion.
    try:
        validate_funnel_recording(funnel)
        row["validation"] = "pass"
        row["stepCount"] = len(funnel["steps"])
        row["selectorSource"] = (
            "css-selectors"
            if all(
                s.get("action") == "click" or len(s.get("selector_candidates") or []) > 0
                for s in funnel["steps"]
            )
            else "missing-selectors"
        )
    except Exception as err:  # noqa: BLE001 - mirrors the JS try/catch around a schema validator
        row["validation"] = "fail"
        row["notes"].append(f"Funnel recording doesn't match our schema: {err}")
        return row

    converted = await convert_funnel_to_script(generation_request=generation_request, funnel=funnel)
    row["conversionUsedMock"] = converted.get("mocked")
    row["script"] = converted.get("script")

    audit1 = await audit_script(script=converted["script"], booking=booking, credentials=credentials, portal_url=portal_url)
    row["auditPrimaryBooking"] = "pass" if audit1.get("pass") else "fail"
    if not audit1.get("pass"):
        row["notes"].append(f"Primary-booking audit: {audit1.get('error')}")

    if second_booking and audit1.get("pass"):
        audit2 = await audit_script(script=converted["script"], booking=second_booking, credentials=credentials, portal_url=portal_url)
        row["auditOverfitCheck"] = "pass" if audit2.get("pass") else "fail"
        if not audit2.get("pass"):
            row["notes"].append(f"Overfit-check audit: {audit2.get('error')}")

    row["overallPass"] = (
        row.get("validation") == "pass"
        and row.get("auditPrimaryBooking") == "pass"
        and row.get("auditOverfitCheck") != "fail"
    )
    return row


def print_score_table(rows: list[dict]) -> None:
    """Simple manual table print (no new dependency) — mirrors console.table's column set."""
    cols = ["tool", "captureStatus", "captureDurationMs", "validation", "stepCount", "auditPrimaryBooking", "auditOverfitCheck", "overallPass"]

    def cell(row: dict, col: str) -> str:
        value = row.get(col, "-")
        return "-" if value is None else str(value)

    widths = {col: max([len(col)] + [len(cell(r, col)) for r in rows]) for col in cols}

    header = " | ".join(col.ljust(widths[col]) for col in cols)
    print(header)
    print("-+-".join("-" * widths[col] for col in cols))
    for row in rows:
        print(" | ".join(cell(row, col).ljust(widths[col]) for col in cols))

    for row in rows:
        notes = row.get("notes") or []
        if notes:
            print(f"\n[{row['tool']}] notes:")
            for note in notes:
                print(f"  - {note}")
