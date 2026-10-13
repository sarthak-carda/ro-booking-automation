"""Stage 2.5 — Automated self-testing/self-repair loop.

Closes a real gap in the pipeline: Stage 3's audit (app/pipeline/audit.py) used to run
exactly once per booking, and a failure immediately marked the whole run "failed" —
single-shot Stage 2 or bust. The Prado/TOSC vendor scripts that actually got working this
cycle only did so because a human iterated by hand: generate -> run for real -> read the
actual error -> fix -> re-run, many rounds. This module automates that loop, bounded.

NOT the same thing as app/self_heal/* — that system is scoped to POST-SHIP drift on an
ALREADY-WORKING script (a vendor tweaks a button's class 6 months later) and is
deliberately narrow: it only ever patches one step's selector via regex string-substitution,
never rewrites the whole script, to keep blast radius tiny and nowhere near payment-safety
logic. This module is for FIRST-TIME generation, where the failure can be structural (a
multi-page dispatch bug, a non-idempotent stepper, a transient disabled-state race) — not
fixable by swapping one selector. It reuses self-heal's diagnose_drift/rewrite_script as a
cheap first attempt when the error happens to be selector-shaped, before falling back to a
broad whole-script repair pass. self_heal/* itself is not modified.

Pure stage module like convert.py/audit.py: no DB writes. The caller (run_pipeline.py)
persists attempt history via its own push_step()/record() into `runs` — repair attempts
belong to the same run's status timeline as capture/convert/audit, not a separate store.
"""
import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from app.pipeline.audit import audit_script
from app.pipeline.convert import repair_funnel_script
from app.self_heal.drift_check import diagnose_drift, parse_failing_step
from app.self_heal.rewrite import rewrite_script

DEFAULT_REPAIR_CAP = 3  # counts real audit_script() calls only -- diagnose_drift/
# repair_funnel_script LLM calls don't hit the vendor site, so they're free w.r.t. this cap.

# Starting guess, not a tuned value -- tune once real rate-limit behavior is observed. A
# tight loop of back-to-back Playwright audits against the same vendor is exactly the burst
# pattern that trips a rate-limiter (directly observed against a real vendor site this week).
REPAIR_BACKOFF_SECONDS = 3

# Checked against error+DOM TOGETHER, not error alone -- a CAPTCHA/anti-bot challenge page
# produces the IDENTICAL symptom as real selector drift (the expected element never renders,
# so the script's own retry exhausts and throws the same "no element matched" shape either
# way). The error string alone cannot distinguish "the vendor changed their page" from "the
# vendor is blocking us" -- that's the whole reason audit.py now returns "dom" on failure.
_BLOCKED_SIGNATURES = [
    (re.compile(r"cf-chl|cf-turnstile|cloudflare", re.I), "cloudflare_challenge"),
    (re.compile(r"g-recaptcha|hcaptcha|h-captcha|data-sitekey", re.I), "captcha_challenge"),
    (re.compile(r"sold[\s-]?out|no longer available|no availability", re.I), "inventory_unavailable"),
    (re.compile(r"\b429\b|too many requests|rate limit", re.I), "rate_limited"),
]


def detect_environment_blocked(*, error: Optional[str], dom: Optional[str]) -> Optional[str]:
    """@returns a short label naming the detected blocker, or None if nothing matched."""
    haystack = f"{error or ''}\n{dom or ''}"
    for pattern, label in _BLOCKED_SIGNATURES:
        if pattern.search(haystack):
            return label
    return None


def _resolve_dom_snapshot(dom_snapshot: Optional[str]) -> Optional[str]:
    """`dom_snapshot` may be a local-filesystem path OR inline HTML (see capture.py's
    docstring). Confirmed: every existing usage (fixtures/funnels/example-vendor.funnel.json,
    both self-heal test files) is a path relative to the pipeline's own working directory on
    the SAME local filesystem, read via a bare read -- no object storage or cross-container
    indirection exists anywhere today. Known v1 boundary: if capture and the pipeline ever
    run in separate containers, or capture moves to object storage, this needs revisiting."""
    if isinstance(dom_snapshot, str) and os.path.isfile(dom_snapshot):
        return Path(dom_snapshot).read_text(encoding="utf8")
    return dom_snapshot


def _reference_dom_for_step(funnel: dict, step_index: int) -> Optional[str]:
    steps = (funnel or {}).get("steps") or []
    if 0 <= step_index < len(steps):
        return _resolve_dom_snapshot(steps[step_index].get("dom_snapshot"))
    return None


async def repair_script(
    *,
    generation_request: dict,
    funnel: dict,
    script: str,
    booking: dict,
    portal_url: str,
    credentials: Optional[dict] = None,
    initial_audit: dict,
    cap: int = DEFAULT_REPAIR_CAP,
    on_attempt: Optional[Callable[[dict], None]] = None,
    booking_label: str = "primary",
) -> dict:
    """@param generation_request - output of context_builder.build_generation_request
    @param funnel - a validated funnel recording (see pipeline/capture.py)
    @param script - the script that failed `initial_audit`
    @param booking - test booking to re-audit against
    @param portal_url
    @param credentials
    @param initial_audit - the audit_script() result that failed, triggering this call
    @param cap - max real audit_script() calls this invocation may make
    @param on_attempt - called once per recorded attempt/outcome, for live visibility
      (a crash mid-loop against a real site is plausible) and for the caller to persist
      (e.g. run_pipeline.py's push_step()). Never raises on the caller's behalf.
    @param booking_label - tags every attempt/outcome record (e.g. "primary"/"overfit") so
      a run with two bookings sharing one attempt budget stays diagnosable after the fact.
    @returns {"outcome": "resolved"|"exhausted"|"environment_blocked",
              "script": str, "audit": dict, "attempts": list[dict]}
    """
    credentials = credentials or {}
    attempts: list = []

    def emit(record: dict) -> dict:
        tagged = {**record, "booking": booking_label}
        attempts.append(tagged)
        if on_attempt:
            on_attempt(tagged)
        return tagged

    current_script = script
    current_audit = initial_audit

    # Checked on the INITIAL audit too, before spending any repair attempt at all -- see
    # module-level comment on _BLOCKED_SIGNATURES for why this can't wait until attempt 1.
    blocked = detect_environment_blocked(error=current_audit.get("error"), dom=current_audit.get("dom"))
    if blocked:
        emit({"attempt": 0, "outcome": "environment_blocked", "blockedReason": blocked, "audit": _summarize(current_audit)})
        return {"outcome": "environment_blocked", "script": current_script, "audit": current_audit, "attempts": attempts}

    narrow_patch_tried = False

    for attempt in range(1, cap + 1):
        mode = "broad"
        patched_script: Optional[str] = None

        if not narrow_patch_tried:
            narrow_patch_tried = True
            failing_step = parse_failing_step(current_audit.get("error"))
            if failing_step:
                try:
                    diagnosis = await diagnose_drift(
                        reference_dom=_reference_dom_for_step(funnel, failing_step["step_index"]) or "",
                        current_dom=current_audit.get("dom") or "",
                        failing_step_index=failing_step["step_index"],
                        failing_selectors=failing_step["tried_candidates"],
                    )
                    rewritten = rewrite_script(
                        script=current_script,
                        step_index=diagnosis.get("step_index", failing_step["step_index"]),
                        diagnosis=diagnosis.get("diagnosis"),
                        suggested_selectors=diagnosis.get("suggested_selectors"),
                    )
                    patched_script = rewritten["script"]
                    mode = "narrow"
                except Exception:
                    # Narrow patch produced nothing usable -- fall through to broad repair
                    # for this attempt and every attempt after (narrow_patch_tried stays True,
                    # so this branch is never retried: the error already proved it can't help).
                    pass

        if patched_script is None:
            repaired = await repair_funnel_script(
                generation_request=generation_request,
                funnel=funnel,
                script=current_script,
                error=current_audit.get("error") or "",
                dom=current_audit.get("dom"),
            )
            patched_script = repaired["script"]

        await asyncio.sleep(REPAIR_BACKOFF_SECONDS)
        new_audit = await audit_script(
            script=patched_script, booking=booking, credentials=credentials, portal_url=portal_url,
        )
        current_script = patched_script
        current_audit = new_audit

        if new_audit.get("pass"):
            emit({"attempt": attempt, "mode": mode, "outcome": "passed", "audit": _summarize(new_audit)})
            return {"outcome": "resolved", "script": current_script, "audit": current_audit, "attempts": attempts}

        blocked = detect_environment_blocked(error=new_audit.get("error"), dom=new_audit.get("dom"))
        if blocked:
            emit({"attempt": attempt, "mode": mode, "outcome": "environment_blocked", "blockedReason": blocked, "audit": _summarize(new_audit)})
            return {"outcome": "environment_blocked", "script": current_script, "audit": current_audit, "attempts": attempts}

        emit({"attempt": attempt, "mode": mode, "outcome": "failed", "audit": _summarize(new_audit)})

    return {"outcome": "exhausted", "script": current_script, "audit": current_audit, "attempts": attempts}


def _summarize(audit: dict) -> dict:
    """Attempt records go into the run's persisted `steps` -- keep them to the fields a
    human/UI actually wants (pass/status/error), not the full DOM snapshot every time."""
    return {"pass": audit.get("pass"), "status": audit.get("status"), "error": audit.get("error")}
