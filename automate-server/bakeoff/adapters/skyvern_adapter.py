"""
Skyvern adapter — implemented against the REST API verified live from
Skyvern-AI/skyvern's `main` branch source on 2026-09-22 (see
bakeoff/README.md). Uses `httpx.AsyncClient` (no `skyvern` pip package
required) so it works identically against a self-hosted instance
(SKYVERN_BASE_URL, e.g. http://localhost:8000, docker-compose'd per their
self-hosted overview docs) or their cloud API — just point SKYVERN_BASE_URL
and SKYVERN_API_KEY at whichever.

VERIFIED (multiple rounds of live probing against the real cloud API, 2026-09-23
— this endpoint has been wrong twice before, so trust the corrections below over
anything in the module's own git history):
  - Round 1 (WRONG, now removed): `/api/v1/run/tasks` — 404s, does not exist.
  - Round 2 (WRONG, superseded below): `POST /api/v1/tasks { url, navigation_goal }`
    -> 200, DOES work, but is Skyvern's OLDER "legacy" task API. Its step
    budget (`max_steps_per_run`, visible on the task object but NOT part of
    the request schema — confirmed by inspecting a real task's echoed
    `request` object) could not be overridden via the request body OR a query
    string in live testing — stayed at the platform default (10) no matter
    what was sent, which is why a real Prado run failed with BUDGET_EXHAUSTED
    after only ~1 step of real progress.
  - Round 3 (CURRENT): `POST {base}/api/v2/tasks { url, user_prompt, max_steps }`
    -> 200, `{"task_id": "tsk_v2_...", "status": "created", "workflow_run_id": ...,
    "workflow_id": ...}` — a newer, workflow-backed task API. Field names
    verified by a 422 response naming the exact missing field (`user_prompt`,
    not `prompt` or `navigation_goal`) when omitted, then a follow-up 200 once
    corrected. `max_steps` accepted without a 422 (not fully confirmed to
    change behavior yet — that needs a real timed-out-vs-not comparison on an
    actual vendor run, not just "the API accepted it").
  - GET {base}/api/v2/tasks/{task_id}           -> status polling for a v2-created
    task (live-verified: returns status "running" for a task just created above).
  - GET {base}/api/v1/tasks/{task_id}/actions   -> list[Action]: action_type,
    xpath, skyvern_element_hash, skyvern_element_data (dict), text/option (for
    fills). Live-verified to work even for a v2-created task_id (200, not 404)
    — v1's actions endpoint is shared across both task generations, unlike
    status polling which has its own v2 path.
  - Auth header is `x-api-key` (live-verified against GET {base}/api/v1/tasks
    and both task-creation endpoints above).
  - Credentials: a vault credential (`credential_id`) attached to the task —
    NOT the raw username/password interpolated into the prompt. UNVERIFIED
    against the v2 schema (no login-required vendor tested against it yet) —
    the field name may differ from `credential_id`; re-check on first use.

NOT IMPLEMENTED (scoped out, not hidden): fetching per-step HTML/screenshot
artifacts (GET {base}/api/v1/tasks/{id}/steps/{step_id}/artifacts, which per the
research DOES expose HTML_ACTION / SCREENSHOT_ACTION / and even a
VISIBLE_ELEMENTS_ID_CSS_MAP artifact — real CSS selectors, if that artifact
type is populated). That's the single most promising lead across all four
original tools for getting a durable CSS selector without our own
XPath-to-CSS workaround, and is the first thing to build out if Skyvern comes
out ahead after this first pass — left out here to keep the initial
comparison run scoped to what's fully verified.

CREDENTIAL HANDLING: if requiresCredentials is true, this REQUIRES
SKYVERN_CREDENTIAL_ID to already exist in Skyvern's vault (created out of
band, e.g. via their UI) — this adapter never sends a raw username/password
in the task prompt, since that would both leak it to the LLM and to Skyvern's
own logs. UNVERIFIED against the corrected `/api/v1/tasks` schema above (no
`credential_id` field was observed in a real task's echoed request object,
since the one real task created so far didn't need credentials) — re-check
this the first time a login-required vendor is run through Skyvern.

Port of bakeoff/adapters/skyvernAdapter.py (the stdin/stdout script version)
into a plain importable async function, called directly in-process by
run_bakeoff.py now that the orchestrator itself is Python — no protocol
overhead, no runtime-conflict reason to keep it out of process (unlike
browser-use/stagehand, see bakeoff/lib/resolve_runtime.py). All endpoint
paths/headers above are unchanged from the verified original.
"""
import asyncio
import os
import time

import httpx


def selector_candidates_from_element_data(data) -> list[str]:
    """CSS-only candidates (our findEl() uses document.querySelector, not XPath)."""
    if not isinstance(data, dict):
        return []
    candidates = []
    # Field names are a best-effort guess at skyvern_element_data's shape —
    # research confirmed the dict exists but not its exact keys.
    if data.get("id"):
        candidates.append(f'#{data["id"]}')
    attrs = data.get("attributes") or {}
    if attrs.get("id") and not data.get("id"):
        candidates.append(f'#{attrs["id"]}')
    if attrs.get("name"):
        candidates.append(f'[name="{attrs["name"]}"]')
    return candidates


async def _api_request(client: httpx.AsyncClient, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    resp = await client.request(method, path, json=body, headers={"x-api-key": api_key})
    resp.raise_for_status()
    return resp.json()


async def run_capture(input_data: dict) -> dict:
    base_url = os.environ.get("SKYVERN_BASE_URL", "https://api.skyvern.com")
    api_key = os.environ["SKYVERN_API_KEY"]

    body = {
        "url": input_data["portalUrl"],
        "user_prompt": input_data["taskPrompt"],
        # Real finding (2026-09-23 Prado run against the OLDER /api/v1/tasks endpoint):
        # its default step budget (10) wasn't enough to get past date selection on a real
        # multi-step funnel, and could not be overridden on that endpoint at all. Moved to
        # /api/v2/tasks (see module docstring) specifically so this is actually honored.
        "max_steps": 40,
    }
    if input_data.get("requiresCredentials"):
        credential_id = os.environ.get("SKYVERN_CREDENTIAL_ID")
        if not credential_id:
            raise RuntimeError(
                "requiresCredentials is true but SKYVERN_CREDENTIAL_ID is not set — "
                "create a credential in Skyvern's vault first, never pass raw credentials in the prompt."
            )
        # UNVERIFIED field name against the real /api/v2/tasks schema -- see module docstring.
        body["credential_id"] = credential_id

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30) as client:
        run = await _api_request(client, api_key, "POST", "/api/v2/tasks", body)
        task_id = run.get("task_id") or run.get("run_id") or run.get("id")
        if not task_id:
            raise RuntimeError(f"Could not find a task id in the run response: {run}")

        deadline = time.monotonic() + 600  # bumped alongside max_steps above
        status = None
        task: dict = {}
        while time.monotonic() < deadline:
            task = await _api_request(client, api_key, "GET", f"/api/v2/tasks/{task_id}")
            status = task.get("status")
            if status in ("completed", "failed", "terminated", "COMPLETE", "TERMINATE", "FAILED"):
                break
            await asyncio.sleep(5)

        if status not in ("completed", "COMPLETE"):
            raise RuntimeError(f"Task did not complete successfully (final status: {status})")

        actions_resp = await _api_request(client, api_key, "GET", f"/api/v1/tasks/{task_id}/actions")

    actions = actions_resp if isinstance(actions_resp, list) else actions_resp.get("actions", [])
    fields_by_value = {str(f["value"]): f for f in input_data["fields"]}
    remaining_fields = list(input_data["fields"])

    steps = []
    for act in actions:
        action_type = str(act.get("action_type", "")).lower()
        is_fill = any(k in action_type for k in ("input", "type", "fill"))
        is_click = "click" in action_type
        if not (is_fill or is_click):
            continue

        field_key = None
        if is_fill:
            typed = act.get("text") or act.get("option")
            matched = fields_by_value.get(str(typed)) if typed is not None else None
            if matched:
                field_key = matched["fieldKey"]
                remaining_fields = [f for f in remaining_fields if f is not matched]
            elif remaining_fields:
                field_key = remaining_fields.pop(0)["fieldKey"]

        steps.append({
            "index": len(steps),
            "action": "fill" if is_fill else "click",
            "url": input_data["portalUrl"],
            "selector_candidates": selector_candidates_from_element_data(act.get("skyvern_element_data")),
            "dom_snapshot": None,  # see module docstring — artifact fetching not implemented yet
            "screenshot": None,
            "field_to_booking_key": field_key,
            "requires_credentials": False,  # handled via credential_id, never surfaced as a fill step
            "credential_key": None,
        })

    return {
        "vendorId": input_data["vendorId"],
        "portalUrl": input_data["portalUrl"],
        "capturedAt": None,
        "loginRequired": bool(input_data.get("requiresCredentials")),
        "steps": steps,
    }


async def run(input: dict) -> dict:  # noqa: A002 - matches the other adapters' `input` param name
    api_key = os.environ.get("SKYVERN_API_KEY")
    if not api_key:
        return {
            "status": "not_configured",
            "reason": "SKYVERN_API_KEY not set. Self-hosted: run `skyvern quickstart` / docker-compose "
                      "(see github.com/Skyvern-AI/skyvern), which provisions a local key. Cloud: get one "
                      "from skyvern.com. Also set SKYVERN_BASE_URL if not using the cloud default.",
        }

    try:
        funnel = await run_capture(input)
        return {"status": "ok", "funnel": funnel}
    except httpx.HTTPStatusError as e:
        return {"status": "error", "error": f"Skyvern API returned {e.response.status_code}: {e.response.text[:500]}"}
    except Exception as e:  # noqa: BLE001 - mirrors the original adapter's catch-all
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
