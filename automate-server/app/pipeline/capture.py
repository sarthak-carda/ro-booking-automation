"""Stage 1 — Cold-start capture.

Ports src/pipeline/capture.js exactly. The actual browser-driving happens
outside this module: an agent (Claude driving a real browser, or a
browser-use script) walks the vendor portal once and records each step.
This module owns the recording's shape and validates it before Stage 2
consumes it — the contract, not the driving.

A funnel recording:
{
  vendorId, portalUrl, capturedAt,
  loginRequired: bool,
  steps: [{
    index, action: "fill"|"click"|"submit"|"navigate",
    url,
    selector_candidates: string[],      # ranked, most-specific first
    dom_snapshot: string,               # path to saved HTML, or inline HTML for small pages
    screenshot: string|None,            # path to saved screenshot, if captured
    field_to_booking_key: string|None,  # dotted path into the booking object (fill steps)
    requires_credentials: bool,
    credential_key: "username"|"password"|None,
  }]
}

Error messages are reproduced verbatim from the Node source — a self-heal
system elsewhere greps the step-level message format, so the text must match
exactly, not just convey the same meaning.
"""
import json
from typing import Any, Union

VALID_ACTIONS = {"fill", "click", "submit", "navigate"}


def load_funnel_recording(source: Union[str, dict]) -> dict:
    if isinstance(source, str):
        with open(source, "r", encoding="utf8") as f:
            recording = json.load(f)
    else:
        recording = source
    validate_funnel_recording(recording)
    return recording


def validate_funnel_recording(recording: Any) -> bool:
    if not recording or not isinstance(recording, dict):
        raise ValueError("Funnel recording must be an object")

    steps = recording.get("steps")
    if not isinstance(steps, list) or len(steps) == 0:
        raise ValueError("Funnel recording must have at least one step")

    for i, step in enumerate(steps):
        action = step.get("action") if isinstance(step, dict) else None
        if action not in VALID_ACTIONS:
            raise ValueError(f'Step {i}: invalid action "{action}"')

        if action == "fill":
            if not step.get("requires_credentials") and not step.get("field_to_booking_key"):
                raise ValueError(f"Step {i}: fill step needs field_to_booking_key or requires_credentials")
            candidates = step.get("selector_candidates")
            if not isinstance(candidates, list) or len(candidates) == 0:
                raise ValueError(f"Step {i}: fill step needs at least one selector candidate")

        if action in ("click", "submit"):
            candidates = step.get("selector_candidates")
            if not isinstance(candidates, list) or len(candidates) == 0:
                raise ValueError(f"Step {i}: {action} step needs at least one selector candidate")

    return True
