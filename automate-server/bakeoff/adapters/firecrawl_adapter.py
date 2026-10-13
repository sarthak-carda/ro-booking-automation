"""
Firecrawl ("Prometheus" internally) adapter — LOWEST CONFIDENCE of the
original four. Research (2026-09-22, see bakeoff/README.md) found Firecrawl
added an `/interact` endpoint that turns a scraped page into a live session
you can drive with inline Playwright-style code, but it does NOT natively
return a selector/DOM/screenshot per action the way the other tools'
history/action APIs do — you have to hand-roll that capture yourself inside
the code you send it. That's what this does.

The research explicitly flagged that the exact request/response shape below
should be sanity-checked against Firecrawl's live OpenAPI spec before
trusting it — this was pulled from doc summarization, not a live API call.
Treat this adapter as a first attempt to get real signal on whether
Firecrawl is viable at all, not a finished integration. If it fails, the
error message IS the finding — that's useful data for the comparison too,
not just a bug to fix.

VERIFIED-ISH (per research, not independently confirmed against a live
account): POST /v2/scrape -> a scrape session; POST
/v2/scrape/{scrapeId}/interact with a code-mode body runs inline
JS/Playwright-ish code against that live session and returns
{ success, cdpUrl, liveViewUrl, output, stdout, result, stderr }.

Port of bakeoff/adapters/firecrawlAdapter.mjs into a plain importable async
function, using httpx.AsyncClient instead of fetch, called directly
in-process by run_bakeoff.py. Endpoint paths/bodies are unchanged.
"""
import os

import httpx

FIRECRAWL_BASE = "https://api.firecrawl.dev"


def _capture_field_code(label: str, value: str) -> str:
    """
    Inline code sent to Firecrawl's code-mode /interact: finds a form field by
    its visible label text, fills it, and returns (via console.log, captured
    as `stdout`/`output` per the endpoint's documented response) the CSS
    selector we can actually use — since Firecrawl gives us no selector back
    on its own, we ask the code we send it to compute one itself.
    """
    import json as _json

    return f"""
const label = {_json.dumps(label)};
const value = {_json.dumps(str(value))};
const input = [...document.querySelectorAll('label')]
  .find(l => l.textContent.trim().startsWith(label))
  ?.querySelector('input, select, textarea');
if (!input) {{ console.log(JSON.stringify({{ found: false }})); }}
else {{
  input.focus();
  input.value = value;
  input.dispatchEvent(new Event('input', {{ bubbles: true }}));
  input.dispatchEvent(new Event('change', {{ bubbles: true }}));
  const candidates = [];
  if (input.id) candidates.push('#' + input.id);
  if (input.name) candidates.push('[name="' + input.name + '"]');
  console.log(JSON.stringify({{ found: true, candidates }}));
}}
""".strip()


def _capture_click_code(description: str) -> str:
    import json as _json

    return f"""
const buttons = [...document.querySelectorAll('button, input[type=submit]')];
const target = buttons.find(b => b.textContent.toLowerCase().includes({_json.dumps(description.lower())}))
  || buttons[buttons.length - 1];
if (!target) {{ console.log(JSON.stringify({{ found: false }})); }}
else {{
  const candidates = [];
  if (target.id) candidates.push('#' + target.id);
  if (target.name) candidates.push('[name="' + target.name + '"]');
  console.log(JSON.stringify({{ found: true, candidates }}));
  // Deliberately NOT clicking here — capture only, never risk submitting a real payment.
}}
""".strip()


def _parse_capture_step(result: dict | None, action: str, field_key: str | None, portal_url: str, index: int) -> dict:
    import json as _json

    candidates: list[str] = []
    try:
        line = ((result or {}).get("stdout") or (result or {}).get("output") or "").strip().split("\n")[-1]
        parsed = _json.loads(line)
        candidates = parsed.get("candidates") or []
    except Exception:
        # Response shape didn't match what we guessed — empty candidates, which
        # will correctly fail our own validation and surface as a real finding.
        pass
    return {
        "index": index,
        "action": action,
        "url": portal_url,
        "selector_candidates": candidates,
        "dom_snapshot": None,
        "screenshot": None,
        "field_to_booking_key": field_key,
        "requires_credentials": False,
        "credential_key": None,
    }


async def _fc(client: httpx.AsyncClient, api_key: str, method: str, path: str, body: dict | None = None):
    resp = await client.request(method, path, json=body, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code >= 400:
        raise RuntimeError(f"Firecrawl API {method} {path} returned {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.text else None


async def run(input: dict) -> dict:  # noqa: A002 - matches the other adapters' `input` param name
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return {"status": "not_configured", "reason": "FIRECRAWL_API_KEY not set. Get one from firecrawl.dev, or self-host per their SELF_HOST.md."}

    try:
        async with httpx.AsyncClient(base_url=FIRECRAWL_BASE, timeout=60) as client:
            scrape = await _fc(client, api_key, "POST", "/v2/scrape", {"url": input["portalUrl"]})
            scrape_id = (scrape or {}).get("id") or (scrape or {}).get("scrapeId") or ((scrape or {}).get("data") or {}).get("id")
            if not scrape_id:
                return {"status": "error", "error": f"Could not find a scrape/session id in the response: {str(scrape)[:500]}"}

            steps = []
            for field in input["fields"]:
                code = _capture_field_code(field["label"], field["value"])
                result = await _fc(client, api_key, "POST", f"/v2/scrape/{scrape_id}/interact", {"type": "code", "language": "javascript", "code": code})
                steps.append(_parse_capture_step(result, "fill", field["fieldKey"], input["portalUrl"], len(steps)))

            final_code = _capture_click_code(((input.get("finalAction") or {}).get("description")) or "the continue button")
            final_result = await _fc(client, api_key, "POST", f"/v2/scrape/{scrape_id}/interact", {"type": "code", "language": "javascript", "code": final_code})
            steps.append(_parse_capture_step(final_result, "click", None, input["portalUrl"], len(steps)))

            try:
                await _fc(client, api_key, "DELETE", f"/v2/scrape/{scrape_id}/interact")
            except Exception:
                pass

        return {
            "status": "ok",
            "funnel": {
                "vendorId": input["vendorId"],
                "portalUrl": input["portalUrl"],
                "capturedAt": None,
                "loginRequired": bool(input.get("requiresCredentials")),
                "steps": steps,
            },
            "raw": {"note": "Low-confidence adapter — see module docstring. If steps look wrong, that's real signal about Firecrawl's fit, not necessarily a bug here."},
        }
    except Exception as err:  # noqa: BLE001 - mirrors the original adapter's catch-all
        return {"status": "error", "error": f"{type(err).__name__}: {err}"}
