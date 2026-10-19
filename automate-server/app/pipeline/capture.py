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
import os
import zipfile
from pathlib import Path
from typing import Any, Optional, Union

VALID_ACTIONS = {"fill", "click", "submit", "navigate"}

# A "reference" is an alternative to a funnel recording: material from a verified run of
# the same flow that Stage 2 reads as ground truth and TRANSLATES into an injectable
# HeadoutAutomation script. It is passed to the model as plain text — no converter program;
# the Stage 2 model is the converter, which is also what keeps loops/waits/conditionals that
# a flat step recording would throw away.
#   {
#     "kind": "prometheus-script" | "devtools-recorder" | "other",
#     "content": str,                # the script / exported recording, verbatim
#     "sampleOutput": str | None,    # optional: what a successful run produced (data.json)
#     "fieldMapping": dict | None,   # optional: input label/flag -> dotted booking key
#   }
REFERENCE_KINDS = {"prometheus-script", "devtools-recorder", "other"}

# Real Prometheus bundles are ~20-35KB and a Recorder export of a full funnel is similar.
# This cap exists so a wrong file (a log, a HAR, a minified bundle) fails loudly here
# instead of silently burning the model's context budget.
MAX_REFERENCE_CHARS = 150_000


def validate_reference(reference: Any) -> dict:
    if not isinstance(reference, dict):
        raise ValueError("Reference must be an object with 'kind' and 'content'")
    kind = reference.get("kind")
    if kind not in REFERENCE_KINDS:
        raise ValueError(f'Reference has invalid kind "{kind}" (expected one of {sorted(REFERENCE_KINDS)})')
    content = reference.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Reference 'content' must be a non-empty string")
    for key in ("content", "sampleOutput"):
        value = reference.get(key)
        if isinstance(value, str) and len(value) > MAX_REFERENCE_CHARS:
            raise ValueError(f"Reference '{key}' is {len(value)} chars (max {MAX_REFERENCE_CHARS}) — wrong file?")
    mapping = reference.get("fieldMapping")
    if mapping is not None and not isinstance(mapping, dict):
        raise ValueError("Reference 'fieldMapping' must be an object of label -> booking key")
    return reference


def load_reference(source: Union[str, dict, None]) -> Optional[dict]:
    """@param source - a reference dict, or a path (see `reference_from_path`), or None"""
    if source is None:
        return None
    if isinstance(source, (str, os.PathLike)):
        return reference_from_path(str(source))
    return validate_reference(source)


def reference_from_path(path: str, field_mapping: Optional[dict] = None) -> dict:
    """Builds a reference from files on disk, so a Prometheus bundle or a DevTools Recorder
    export can be handed over exactly as received:
      - a directory (or .zip) containing `script.ts` (+ optional `data.json`) → prometheus-script
      - a .json file with a top-level "steps" list (Chrome DevTools Recorder export) → devtools-recorder
    """
    p = Path(path)
    if p.is_dir():
        script = p / "script.ts"
        if not script.is_file():
            raise ValueError(f"{path}: no script.ts found — not a Prometheus bundle?")
        sample = p / "data.json"
        return validate_reference({
            "kind": "prometheus-script",
            "content": script.read_text(encoding="utf8"),
            "sampleOutput": sample.read_text(encoding="utf8") if sample.is_file() else None,
            "fieldMapping": field_mapping,
        })

    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as zf:
            names = {Path(n).name: n for n in zf.namelist() if not n.endswith("/")}
            if "script.ts" not in names:
                raise ValueError(f"{path}: zip has no script.ts — not a Prometheus bundle?")
            return validate_reference({
                "kind": "prometheus-script",
                "content": zf.read(names["script.ts"]).decode("utf8"),
                "sampleOutput": zf.read(names["data.json"]).decode("utf8") if "data.json" in names else None,
                "fieldMapping": field_mapping,
            })

    if p.is_file() and p.suffix == ".json":
        text = p.read_text(encoding="utf8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as err:
            raise ValueError(f"{path}: not valid JSON ({err})") from err
        if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
            return validate_reference({"kind": "devtools-recorder", "content": text, "fieldMapping": field_mapping})
        raise ValueError(f"{path}: JSON has no top-level 'steps' list — not a DevTools Recorder export?")

    raise ValueError(f"{path}: expected a Prometheus bundle (dir/zip with script.ts) or a Recorder .json export")


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
