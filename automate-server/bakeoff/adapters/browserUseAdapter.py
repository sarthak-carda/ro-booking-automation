#!/usr/bin/env python3
"""
browser-use adapter — implemented against browser-use v0.13.10, verified
against actual library source on 2026-09-22 (see bakeoff/README.md for the
research notes). Runs a real Agent against the vendor portal and converts
its action history into our funnel-recording shape.

VERIFIED (from source, not guessed):
  - pip install browser-use==0.13.10
  - Agent(task=<string, URL auto-detected inside it>, llm=ChatOpenAI(...), sensitive_data={...})
  - history = await agent.run(); history.action_history() -> list[list[dict]],
    each dict has "interacted_element" (DOMInteractedElement: x_path, attributes dict)
  - history.screenshots() -> base64 PNG per step

NOT VERIFIED / INFERRED (flagged so a real run can correct it fast):
  - The exact dict keys used below for the action's type ("action"/"type") and
    typed value ("value"/"text") on each `act` entry in action_history() —
    the research pass confirmed `interacted_element` and `result` exist, but
    did not enumerate every key. This code defensively checks a few
    plausible names; if a real run's output doesn't match, `--dump-raw` mode
    (set BAKEOFF_DUMP_RAW=1) prints the raw history to stderr for inspection.

KNOWN LIMITATION (real finding, not a bug to hide): DOMInteractedElement
exposes an XPath + raw attributes, but no ready CSS selector, and there is no
full-page DOM snapshot — only the interacted element's own attributes and a
screenshot. Our generated scripts locate elements via
`document.querySelector(candidate)` (see src/openai/mock.js /
pipeline/convert.js's contract), which does not understand XPath. So a step
whose element has no id/name/class/placeholder attribute ends up with an
EMPTY selector_candidates list here — which will correctly fail our own
capture.js validation. That's a real signal about this tool's fit for our
exact contract, not something to paper over.
"""
import asyncio
import base64
import json
import os
import sys


def emit(obj):
    print(json.dumps(obj))
    sys.stdout.flush()


def debug(msg):
    if os.environ.get("BAKEOFF_DUMP_RAW"):
        print(msg, file=sys.stderr)


def selector_candidates_from_element(el):
    """CSS-only candidates (our findEl() uses document.querySelector, not XPath)."""
    if not el:
        return []
    attrs = el.get("attributes") or {}
    candidates = []
    if attrs.get("id"):
        candidates.append(f'#{attrs["id"]}')
    if attrs.get("name"):
        candidates.append(f'[name="{attrs["name"]}"]')
    if attrs.get("placeholder"):
        candidates.append(f'[placeholder="{attrs["placeholder"]}"]')
    if attrs.get("class"):
        cls = ".".join(c for c in attrs["class"].split() if c)
        if cls:
            candidates.append(f".{cls}")
    return candidates


def save_screenshot(b64, out_dir, index):
    if not b64:
        return None
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"step-{index}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path
    except Exception:
        return None


async def run_capture(input_data):
    from browser_use import Agent, ChatOpenAI

    llm = ChatOpenAI(model=os.environ.get("BROWSER_USE_MODEL", "gpt-4.1"))

    sensitive_data = None
    task_prompt = input_data["taskPrompt"]
    if input_data.get("requiresCredentials") and input_data.get("credentials"):
        sensitive_data = {
            "x_user": input_data["credentials"]["username"],
            "x_pass": input_data["credentials"]["password"],
        }
        task_prompt += "\nWhen logging in, type x_user into the username/email field and x_pass into the password field."

    agent = Agent(
        task=f'Go to {input_data["portalUrl"]} . {task_prompt}',
        llm=llm,
        sensitive_data=sensitive_data,
    )
    history = await agent.run()
    debug(f"RAW action_history: {history.action_history()!r}")

    screenshots = history.screenshots()
    screenshot_dir = os.environ.get("BAKEOFF_SCREENSHOT_DIR", "/tmp/automate-bakeoff/browser-use")

    # Match a fill action's typed value back to a known field by exact value match
    # first (reliable when it works); fall back to consuming fields in the order
    # the task prompt listed them (browser-use tends to follow the prompt's order).
    fields_by_value = {str(f["value"]): f for f in input_data["fields"]}
    remaining_fields = list(input_data["fields"])
    credential_keys_used = []

    steps = []
    for i, step_actions in enumerate(history.action_history() or []):
        for act in step_actions or []:
            el = act.get("interacted_element") if isinstance(act, dict) else None
            action_label = str(act.get("action") or act.get("type") or act.get("name") or "").lower()
            is_fill = any(k in action_label for k in ("type", "input", "fill"))
            is_click = "click" in action_label
            if not (is_fill or is_click):
                continue

            field_key = None
            requires_credentials = False
            credential_key = None
            if is_fill:
                typed = act.get("value", act.get("text"))
                matched = fields_by_value.get(str(typed)) if typed is not None else None
                if matched:
                    field_key = matched["fieldKey"]
                    remaining_fields = [f for f in remaining_fields if f is not matched]
                elif input_data.get("requiresCredentials") and typed in (
                    input_data.get("credentials", {}).get("username"),
                    input_data.get("credentials", {}).get("password"),
                ):
                    requires_credentials = True
                    credential_key = "username" if "username" not in credential_keys_used else "password"
                    credential_keys_used.append(credential_key)
                elif remaining_fields:
                    field_key = remaining_fields.pop(0)["fieldKey"]

            steps.append({
                "index": len(steps),
                "action": "fill" if is_fill else "click",
                "url": input_data["portalUrl"],
                "selector_candidates": selector_candidates_from_element(el),
                "dom_snapshot": None,
                "screenshot": save_screenshot(screenshots[i] if i < len(screenshots) else None, screenshot_dir, i),
                "field_to_booking_key": field_key,
                "requires_credentials": requires_credentials,
                "credential_key": credential_key,
            })

    return {
        "vendorId": input_data["vendorId"],
        "portalUrl": input_data["portalUrl"],
        "capturedAt": None,
        "loginRequired": bool(input_data.get("requiresCredentials")),
        "steps": steps,
    }


def main():
    input_data = json.loads(sys.stdin.read())

    if not os.environ.get("OPENAI_API_KEY"):
        return emit({"status": "not_configured", "reason": "OPENAI_API_KEY not set — browser-use needs an LLM as its 'brain'."})

    try:
        import browser_use  # noqa: F401
    except ImportError:
        return emit({"status": "not_configured", "reason": "browser-use is not installed. Run: pip install browser-use==0.13.10"})

    try:
        funnel = asyncio.run(run_capture(input_data))
        emit({"status": "ok", "funnel": funnel})
    except Exception as e:
        emit({"status": "error", "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
