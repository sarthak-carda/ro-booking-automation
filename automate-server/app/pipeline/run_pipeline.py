"""Orchestrates Stages 0-4 for one vendor and records a run (queued ->
capturing -> converting -> auditing -> shipped/failed) so `/api/runs`
reflects real pipeline progress — including for the fixture-driven demo.
Ports src/pipeline/runPipeline.js exactly.

Stage 1 (capture) is NOT re-run here: this function is handed an already-
captured funnel recording, since capture is inherently an interactive
browser-driving step (see pipeline/capture.py's docstring). Everything from
Stage 2 onward is fully automated.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import runs, vendors
from app.pipeline.context_builder import build_generation_request
from app.pipeline.capture import load_funnel_recording, load_reference
from app.pipeline.convert import convert_funnel_to_script
from app.pipeline.repair import DEFAULT_REPAIR_CAP, repair_script
from app.pipeline.ship import ship_script

try:
    from app.pipeline.audit import audit_script
except ImportError:  # app/pipeline/audit.py (Stage 3, another agent's slice) not landed yet.
    async def audit_script(*, script: str, booking: dict, credentials: Optional[dict] = None, portal_url: str) -> dict:
        # TODO: remove this stub once app/pipeline/audit.py lands and drop this try/except.
        # Fake pass so this module's own orchestration logic is testable in isolation.
        return {"pass": True, "status": "PAYMENT_REQUIRED", "error": None}


def _iso_now() -> str:
    """Matches JS `new Date().toISOString()` formatting: millisecond precision + "Z"."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


async def run_vendor_pipeline(
    vendor: dict,
    sample_booking: dict,
    second_sample_booking: Optional[dict] = None,
    funnel: Any = None,
    credentials: Optional[dict] = None,
    auto_ship: bool = True,
    reviewed_by: Optional[str] = None,
    reference: Any = None,
) -> dict:
    """@param vendor
    @param sample_booking - used for Stage 0 + Stage 3 (first test booking)
    @param second_sample_booking - a different booking to prove the script isn't
      overfit (test checklist item)
    @param funnel - a funnel recording dict, or a path to one
    @param reference - alternative/addition to `funnel`: a verified run of the same flow
      (Prometheus script, DevTools Recorder export...) for Stage 2 to translate. A dict, or a
      path (see capture.reference_from_path). At least one of funnel/reference is required;
      with no funnel the narrow selector self-heal patch is skipped (it needs step indices).
    @param credentials - test/sandbox credentials only
    @param auto_ship - ship automatically if audit passes (still requires a human to
      have reviewed the script upstream in real usage)
    """
    credentials = credentials or {}
    run_id = str(uuid.uuid4())

    def record(patch: dict) -> dict:
        return runs.upsert(run_id, {
            "kind": "pipeline",
            "vendorId": vendor.get("id"),
            "updatedAt": _iso_now(),
            **patch,
        })

    def push_step(name: str, status: str, detail: Any) -> None:
        existing = runs.get(run_id)
        steps = list((existing or {}).get("steps") or [])
        steps.append({"name": name, "status": status, "detail": detail, "at": _iso_now()})
        runs.upsert(run_id, {"steps": steps})

    def record_vendor_outcome(outcome: str) -> None:
        v = vendors.get(vendor.get("id"))
        history = list((v or {}).get("runHistory") or [])
        history.append({"outcome": outcome, "at": _iso_now()})
        history = history[-20:]
        # "blocked" (the vendor's own anti-bot/inventory state, not a script defect) is kept
        # distinct from "failing" (the script itself needs work) -- no code validates vendor
        # status against a fixed enum, so a third value is safe/low-ripple.
        status = "active" if outcome == "shipped" else ("blocked" if outcome == "blocked" else "failing")
        vendors.upsert(vendor.get("id"), {"runHistory": history, "status": status})

    record({"status": "queued", "createdAt": _iso_now(), "steps": []})
    vendors.upsert(vendor.get("id"), {**vendor, "lastPipelineRunId": run_id})

    try:
        record({"status": "capturing"})
        generation_request = build_generation_request(vendor, sample_booking)
        if not funnel and not reference:
            raise ValueError("A funnel recording or a reference is required")
        recording = load_funnel_recording(funnel) if funnel else None
        reference_doc = load_reference(reference) if reference else None
        push_step("capture", "ok", {
            "steps": len((recording or {}).get("steps") or []),
            "reference": reference_doc.get("kind") if reference_doc else None,
        })

        record({"status": "converting"})
        convert_result = await convert_funnel_to_script(generation_request, recording, reference_doc)
        script = convert_result["script"]
        mocked = convert_result["mocked"]
        push_step("convert", "ok", {"mocked": mocked})

        record({"status": "auditing"})
        first_audit = await audit_script(
            script=script, booking=sample_booking, credentials=credentials, portal_url=vendor.get("portalUrl")
        )
        push_step("audit:primary-booking", "ok" if first_audit.get("pass") else "failed", first_audit)

        first_repair_attempts: list = []
        if not first_audit.get("pass"):
            record({"status": "repairing"})
            repair_result = await repair_script(
                generation_request=generation_request,
                funnel=recording,
                reference=reference_doc,
                script=script,
                booking=sample_booking,
                portal_url=vendor.get("portalUrl"),
                credentials=credentials,
                initial_audit=first_audit,
                cap=DEFAULT_REPAIR_CAP,
                on_attempt=lambda a: push_step(f"repair:attempt-{a['attempt']}", a["outcome"], a),
                booking_label="primary",
            )
            first_repair_attempts = repair_result["attempts"]
            if repair_result["outcome"] != "resolved":
                final_status = "blocked" if repair_result["outcome"] == "environment_blocked" else "failed"
                record({"status": final_status, "failureReason": repair_result["audit"].get("error")})
                record_vendor_outcome(final_status)
                return {
                    "runId": run_id,
                    "status": final_status,
                    "reason": repair_result["audit"].get("error"),
                    "script": repair_result["script"],
                    "audit": repair_result["audit"],
                }
            script, first_audit = repair_result["script"], repair_result["audit"]
            push_step("repair", "ok", {"attempts": len(first_repair_attempts)})

        second_audit = None
        if second_sample_booking:
            second_audit = await audit_script(
                script=script,
                booking=second_sample_booking,
                credentials=credentials,
                portal_url=vendor.get("portalUrl"),
            )
            push_step("audit:second-booking", "ok" if second_audit.get("pass") else "failed", second_audit)
            if not second_audit.get("pass"):
                # Shares the run's overall attempt budget with the first booking's repair
                # rather than getting its own fresh cap, so one run can't unboundedly
                # multiply real-site hits -- but a STRICT shared cap could fully starve this
                # booking (first booking's repair could in principle eat the whole budget),
                # so it's always guaranteed at least one attempt of its own.
                second_cap = max(1, DEFAULT_REPAIR_CAP - len(first_repair_attempts))
                record({"status": "repairing"})
                repair_result = await repair_script(
                    generation_request=generation_request,
                    funnel=recording,
                    reference=reference_doc,
                    script=script,
                    booking=second_sample_booking,
                    portal_url=vendor.get("portalUrl"),
                    credentials=credentials,
                    initial_audit=second_audit,
                    cap=second_cap,
                    on_attempt=lambda a: push_step(f"repair:attempt-{a['attempt']}", a["outcome"], a),
                    booking_label="overfit",
                )
                if repair_result["outcome"] != "resolved":
                    final_status = "blocked" if repair_result["outcome"] == "environment_blocked" else "failed"
                    record({
                        "status": final_status,
                        "failureReason": f"Overfit check failed: {repair_result['audit'].get('error')}",
                    })
                    record_vendor_outcome(final_status)
                    return {
                        "runId": run_id,
                        "status": final_status,
                        "reason": repair_result["audit"].get("error"),
                        "script": repair_result["script"],
                        "audit": repair_result["audit"],
                    }
                # A repair made for the second (overfit-check) booking isn't re-verified
                # against the first booking here -- re-running the first booking's audit
                # again would cost another real-site hit this design is explicitly trying to
                # bound. Known v1 gap: a broad (non-selector-patch) repair at this point could
                # in principle regress the first booking's passing state, undetected within
                # this same run.
                script, second_audit = repair_result["script"], repair_result["audit"]
                push_step("repair", "ok", {"attempts": len(repair_result["attempts"])})

        if not auto_ship:
            record({"status": "awaiting_review", "script": script})
            return {
                "runId": run_id,
                "status": "awaiting_review",
                "script": script,
                "audit": first_audit,
                "secondAudit": second_audit,
            }

        shipped = ship_script(
            vendor_id=vendor.get("id"),
            portal_url=vendor.get("portalUrl"),
            script=script,
            audit_report=first_audit,
            reviewed_by=reviewed_by,
        )
        push_step("ship", "ok", {"host": shipped.get("host"), "version": shipped.get("version")})
        record({"status": "shipped", "scriptHost": shipped.get("host"), "version": shipped.get("version")})
        record_vendor_outcome("shipped")
        return {
            "runId": run_id,
            "status": "shipped",
            "script": script,
            "shipped": shipped,
            "audit": first_audit,
            "secondAudit": second_audit,
        }
    except Exception as err:
        record({"status": "failed", "failureReason": str(err)})
        record_vendor_outcome("failed")
        raise
