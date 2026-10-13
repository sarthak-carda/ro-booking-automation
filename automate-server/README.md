# automate-server

Script-resolution + script-generation service for the Headout RO automation
Chrome extension (`ro-automation`, the parent repo). Implements the pipeline
from the RO Automation plan (`pipeline-04-ro-build-test`): §5 (Stage 0-4
script-generation pipeline), §6 (Keeper credential seam), §8 (self-heal loop),
§9 (threshold-based eligibility), §10 (Selene API surface).

**Built on FastAPI/Python, not Node.js.** The service was originally built in
Node/Express, fully tested, then ported to FastAPI once that became a hard
requirement — the Node implementation has been retired. The one place Node.js
still appears is `bakeoff/adapters/stagehandAdapter.mjs`, since Stagehand is a
Node-only library with no Python equivalent — see
[bakeoff/README.md](bakeoff/README.md).

See [bakeoff/README.md](bakeoff/README.md) for the Stage 1 capture-tool
comparison (browser-use vs. Stagehand vs. Skyvern vs. Firecrawl vs. Claude +
Chrome DevTools MCP) — a separate harness that reuses this service's Stage
2/3 pipeline to score each tool objectively.

## Status (read this before assuming anything is "real")

| Integration | Status | Where |
|---|---|---|
| Script resolve/serve (`/api/scripts/resolve`) | **Real** | `app/routes/scripts.py` |
| Stage 0-4 pipeline orchestration | **Real** | `app/pipeline/run_pipeline.py` |
| Stage 3 audit gate (headless Playwright replay) | **Real** | `app/pipeline/audit.py` |
| Self-heal loop (drift-check → rewrite → cap → escalate) | **Real** | `app/self_heal/loop.py` |
| Threshold-eligibility logic | **Real** | `app/threshold/check_threshold.py` |
| Selene API surface (§10) | **Real** | `app/routes/*` |
| Stage 2 script generation | **Real, live-verified** — OpenAI (`gpt-5.6-sol`, confirmed against a real key) or Anthropic, selectable via `LLM_PROVIDER`; falls back to a deterministic offline template generator when no key is configured | `app/llm/client.py`, `app/llm/mock.py` |
| Aries booking data | **Stubbed with fixtures** — no live Aries access this pass | `fixtures/bookings/*.json` |
| Keeper credentials | **Stubbed with fixtures** — no live Keeper access this pass | `app/keeper/keeper_client.py`, `fixtures/keeper/test-credentials.json` |
| Zendesk manual-ticket pull | **Stubbed with a fixture** — no live Zendesk access this pass | `app/zendesk/zendesk_fixture.py`, `fixtures/zendesk-tickets.fixture.json` |
| 6 pilot vendors (real portal URLs, login confirmation, real captures) | **Not started** — `fixtures/vendors.json` has all 6 with `portalUrl: null`, awaiting real data | — |

Every stub above is a real module with a real function signature; swapping in
live access means implementing the body of one function, not restructuring
callers. See each file's docstring for the exact seam.

**Nothing in this codebase ever completes a real vendor payment.** The audit
gate (Stage 3) and self-heal's re-audit both assert the script stops at
`PAYMENT_REQUIRED` and fail loudly otherwise — see `app/pipeline/audit.py`.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/playwright install chromium   # needed once, for the Stage 3 audit gate
cp .env.example .env                      # fill in OPENAI_API_KEY / ANTHROPIC_API_KEY when you have them
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 3000   # matches config.js in the extension
```

```bash
# Run the test suite per-file, not `pytest tests_py/` in one shot — Python's
# module-level singletons (app.config, app.db) don't get per-file process
# isolation the way Node's `node --test` did. See tests_py/test_threshold.py's
# docstring for the full explanation.
for f in tests_py/test_*.py; do ./.venv/bin/pytest "$f" -v; done
```

Node.js is only needed if you're also running the Stage 1 bake-off's
Stagehand adapter or `chrome-devtools-mcp` (via `npx`) — not for running this
service itself.

## How the offline "example vendor" proves the mechanism

Real vendor captures (Stage 1) are inherently interactive — an agent (Claude
driving a real browser, or a browser-use script) has to actually walk the
portal once. Rather than fake that with a placeholder, `fixtures/` ships a
tiny **real** offline checkout page (`fixtures/html/example-vendor/`) served
by a local static file server in tests — the pipeline runs against it exactly
as it would against a real vendor, just without the real-world risk. This is
what the `tests_py/` suite exercises, and it's how every item in the plan's
test checklist (§12) that doesn't require the real 6 vendors is demonstrated:

- Stage 0-4 shipping a real, inspectable script (`tests_py/test_pipeline.py`)
- The same script working on a second, different booking — proves it's not
  overfit to one payload (`tests_py/test_pipeline.py`)
- The audit gate failing loudly, not silently, on a broken page
  (`tests_py/test_api_routes.py`, `test_pipeline.py`)
- Self-heal detecting a deliberately-drifted field, diagnosing it (via a
  stable `name` attribute or, failing that, the field's `<label>` text — see
  `app/llm/mock.py`), patching just that one step, and re-shipping
  (`tests_py/test_self_heal.py`)
- Self-heal escalating instead of looping forever when no fix is found
  (`tests_py/test_self_heal.py`)
- The threshold job flagging vendor/tour pairs from fixture data, respecting
  a configurable threshold, and not re-queuing an already-shipped vendor
  (`tests_py/test_threshold.py`)

## Wiring in the 6 real pilot vendors

For each vendor in `fixtures/vendors.json`:

1. Confirm login requirement by actually visiting the portal (plan §4 — don't
   guess). Fill in `portalUrl` and `requiresCredentials`.
2. Capture the funnel (Stage 1): drive the real portal once — using one of
   the tools compared in [bakeoff/](bakeoff/README.md) — and record each step
   in the shape documented in `app/pipeline/capture.py` — selector
   candidates, the DOM at that step, and which booking field it fills. Save
   it as `fixtures/funnels/<vendor>.funnel.json`.
3. Get a real sample booking shaped like `fixtures/bookings/*.json` (replace
   the constructed approximation there once real Aries access exists).
4. Call the pipeline:
   ```python
   from app.pipeline.run_pipeline import run_vendor_pipeline
   await run_vendor_pipeline(vendor=vendor, sample_booking=sample_booking, second_sample_booking=second, funnel=funnel, credentials=credentials, auto_ship=False)
   ```
   `auto_ship=False` stops after Stage 3 so a human reviews the generated
   script (plan §12 requires this) before it ships — or drive the same thing
   through `POST /api/eligible-vendors/{vendor_id}/trigger`.
5. Never use a real vendor credential for a capture/audit run — only
   test/sandbox credentials (plan §7).

## API surface (for Selene)

All responses are `{ data: ... }` on success, `{ error: "..." }` on failure.

- `GET /api/scripts/resolve?url=<vendorLink>&format=js` — raw JS if `format=js`
  (what the extension's `fetchAutofillScript` expects — it does `res.text()`),
  otherwise a JSON envelope. 404 if nothing's shipped for that host.
- `GET /api/vendors` / `GET /api/vendors/{id}` — status, success rate, last N
  run outcomes.
- `GET /api/runs?vendorId=` / `GET /api/runs/{id}` — pipeline run history:
  `queued → capturing → converting → auditing → shipped/failed`, each with a
  `steps` array.
- `GET /api/self-heal-queue?status=` / `GET /api/self-heal-queue/{id}` — items
  needing triage, with attempt history and diagnoses.
- `POST /api/self-heal-queue/report` — an RO person's post-payment "no" (plan
  §8 trigger (b)); body: `{ vendorId, portalUrl, runId?, referenceScreenshot?, failureScreenshot?, note? }`.
- `GET /api/eligible-vendors` — vendor/tour pairs at or above
  `AUTOMATION_THRESHOLD` from the (currently fixture) Zendesk pull.
- `POST /api/eligible-vendors/{vendor_id}/trigger` — manually run the Stage 0-4
  pipeline for a vendor; body: `{ sampleBooking, secondSampleBooking?, funnel, credentials?, autoShip? }`.

## Config

See `.env.example`. Key ones: `OPENAI_API_KEY` / `OPENAI_MODEL`,
`ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`, `LLM_PROVIDER` (`openai` or
`anthropic` — picks Stage 2 + self-heal drift-check's backend),
`AUTOMATION_THRESHOLD` (plan §9 — directionally 30, changeable without a code
change), `KEEPER_STUB` / `ZENDESK_STUB` (flip to `false` once real
integrations are implemented in `app/keeper/keeper_client.py` /
`app/zendesk/zendesk_fixture.py`).
