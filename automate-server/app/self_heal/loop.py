"""Self-heal loop (plan §8). Ports src/selfHeal/loop.js exactly.

Trigger (a): a Stage-3-style audit failure (script errors, or fails to reach
PAYMENT_REQUIRED) on a live script during a re-check.
Trigger (b): an RO person's post-payment "did it work? No" — see
`trigger_from_ro_report` below. That report carries no structural signal (no
failing selector), so it's queued straight to manual triage rather than
guessed at automatically; trigger (a)'s audit failure is what the automated
drift-check/rewrite loop below actually runs against.

Cap: 2-3 automated retries (default 3), tracked per attempt in the queue item
so `/api/self-heal-queue` has real attempt history to show.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import self_heal_queue, vendors
from app.pipeline.audit import audit_script
from app.pipeline.ship import ship_script
from app.self_heal.drift_check import diagnose_drift, parse_failing_step
from app.self_heal.rewrite import rewrite_script

DEFAULT_CAP = 3


def _iso_now() -> str:
    """Matches JS `new Date().toISOString()` formatting: millisecond precision + "Z"."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


async def run_self_heal(
    *,
    vendor_id: Any,
    portal_url: str,
    script: str,
    failure_error: str,
    booking: dict,
    credentials: Optional[dict] = None,
    reference_dom: str,
    current_dom: str,
    cap: int = DEFAULT_CAP,
) -> dict:
    """@param vendor_id
    @param portal_url
    @param script - the currently-shipped (failing) script
    @param failure_error - the audit/run error message
    @param booking - test booking to re-audit against
    @param credentials
    @param reference_dom - DOM snapshot from the original Stage 1 capture, for the failing step
    @param current_dom - DOM snapshot taken at the point of failure
    @param cap
    """
    credentials = credentials or {}
    item_id = str(uuid.uuid4())
    attempts: list = []

    def record(patch: dict) -> dict:
        return self_heal_queue.upsert(
            item_id,
            {"vendorId": vendor_id, "portalUrl": portal_url, "attempts": attempts, "cap": cap, **patch},
        )

    record({"status": "healing", "trigger": "audit-failure", "startedAt": _iso_now()})

    current_script = script
    current_error = failure_error
    current_dom_snapshot = current_dom

    for attempt in range(1, cap + 1):
        failing_step = parse_failing_step(current_error)
        if not failing_step:
            record({
                "status": "escalated",
                "escalationReason": f"Error not selector-shaped, cannot auto-diagnose: {current_error}",
            })
            _mark_vendor_needs_review(vendor_id)
            return self_heal_queue.get(item_id)

        # diagnose_drift/rewrite_script now fail LOUDLY by design (no diagnosis,
        # or the script no longer matches the expected findEl contract) rather
        # than silently returning an empty suggestion — so a raised exception
        # here is a genuine "this attempt didn't produce anything" outcome, not
        # a bug. Record it and let the loop's cap turn repeated failures into an
        # escalation below, the same as a patched-but-still-failing audit.
        try:
            diagnosis = await diagnose_drift(
                reference_dom=reference_dom,
                current_dom=current_dom_snapshot,
                failing_step_index=failing_step["step_index"],
                failing_selectors=failing_step["tried_candidates"],
            )

            rewritten = rewrite_script(
                script=current_script,
                step_index=diagnosis.get("step_index", failing_step["step_index"]),
                diagnosis=diagnosis.get("diagnosis"),
                suggested_selectors=diagnosis.get("suggested_selectors"),
            )

            audit = await audit_script(
                script=rewritten["script"], booking=booking, credentials=credentials, portal_url=portal_url
            )
            attempts.append({
                "attempt": attempt,
                "diagnosis": diagnosis,
                "patched": True,
                "audit": {"pass": audit["pass"], "status": audit["status"], "error": audit.get("error")},
            })
            record({"attempts": attempts})

            if audit["pass"]:
                shipped = ship_script(
                    vendor_id=vendor_id,
                    portal_url=portal_url,
                    script=rewritten["script"],
                    audit_report=audit,
                    reviewed_by="self-heal",
                )
                record({"status": "resolved", "resolvedAt": _iso_now(), "shippedVersion": shipped.get("version")})
                _mark_vendor_healed(vendor_id)
                return self_heal_queue.get(item_id)

            current_script = rewritten["script"]
            current_error = audit.get("error") or current_error
        except Exception as err:
            attempts.append({"attempt": attempt, "outcome": "diagnose-or-patch-failed", "error": str(err)})
            record({"attempts": attempts})
            # current_script/current_error unchanged — the next attempt retries
            # diagnosis from scratch against the same failure (worthwhile with a
            # real, non-deterministic model; a script-contract mismatch will keep
            # failing identically and correctly exhaust the cap into escalation
            # below).

    record({
        "status": "escalated",
        "escalationReason": f"Exhausted {cap} automated retries",
        "finalScript": current_script,
    })
    _mark_vendor_needs_review(vendor_id)
    return self_heal_queue.get(item_id)


def trigger_from_ro_report(
    *,
    vendor_id: Any,
    portal_url: str,
    run_id: Optional[str] = None,
    reference_screenshot: Optional[str] = None,
    failure_screenshot: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Trigger (b): an RO person reports a completed run didn't actually work.
    No structural diff is available from a report alone, so this queues
    straight to manual triage. Always escalates immediately (cap: 0, no
    diagnosis attempted)."""
    item_id = str(uuid.uuid4())
    self_heal_queue.set(
        item_id,
        {
            "vendorId": vendor_id,
            "portalUrl": portal_url,
            "runId": run_id,
            "trigger": "ro-report",
            "status": "escalated",
            "escalationReason": note or "RO reported payment did not go through; no structural signal to auto-diagnose",
            "referenceScreenshot": reference_screenshot,
            "failureScreenshot": failure_screenshot,
            "attempts": [],
            "cap": 0,
            "startedAt": _iso_now(),
        },
    )
    _mark_vendor_needs_review(vendor_id)
    return self_heal_queue.get(item_id)


def _mark_vendor_needs_review(vendor_id: Any) -> None:
    vendor = vendors.get(vendor_id)
    if vendor:
        vendors.upsert(vendor_id, {"status": "needs_review"})


def _mark_vendor_healed(vendor_id: Any) -> None:
    vendor = vendors.get(vendor_id)
    if vendor:
        vendors.upsert(vendor_id, {"status": "active"})
