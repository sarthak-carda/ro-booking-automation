# Stage 1 capture-tool bake-off

Compares browser-use, Stagehand, Skyvern, Firecrawl, and Claude + Chrome
DevTools MCP as candidates for Stage 1 (funnel capture) of the RO automation
pipeline — see the parent [README.md](../README.md) and the plan's §5/§11.
Nothing here is guesswork about which tool is "best" — every adapter's
output is run through the **same, real Stage 2 (convert) + Stage 3 (audit)
pipeline** the production service uses, so the comparison is an objective
pass/fail, not a vibes-based writeup: whichever tool's captured funnel
recording actually survives the audit gate (reaches `PAYMENT_REQUIRED`, on
two different bookings) wins on the criterion that matters most.

## Orchestrator is now Python

The harness was ported from Node to Python (`bakeoff/run_bakeoff.py`
replaces `runBakeoff.js` as the entrypoint) so it lives next to the rest of
this service's Python port. Two consequences worth knowing:

- **mock, skyvern, and firecrawl** are now plain importable
  `async def run(input: dict) -> dict` functions
  (`bakeoff/adapters/mock_adapter.py` / `skyvern_adapter.py` /
  `firecrawl_adapter.py`), called directly in-process — no subprocess, no
  JSON-over-stdio overhead.
- **browser-use, stagehand, and the new claude-devtools** adapter still run
  as subprocesses over the original JSON-stdin / one-JSON-line-stdout
  protocol (`bakeoff/lib/run_adapter.py`'s `run_subprocess_adapter`),
  because each has its own reason it can't join the main process:
  browser-use needs `bakeoff/.venv`'s dependency-isolated packages, Stagehand
  is a Node-only library with no Python equivalent, and claude-devtools
  needs `anthropic` + `mcp` (from the MAIN `.venv`) while also launching its
  own `npx chrome-devtools-mcp` child process — kept subprocess-isolated for
  lifecycle/timeout handling identical to the other two rather than a
  special case in the orchestrator.
- **Node.js is no longer used for orchestration at all.** It's now only
  used for (a) `stagehandAdapter.mjs` itself, run under a Node≥22 binary
  resolved via `bakeoff/lib/resolve_runtime.py`'s `node22_binary()`, and (b)
  launching `chrome-devtools-mcp` via `npx` inside
  `claude_devtools_adapter.py`.

`runBakeoff.js` and its `bakeoff/lib/*.js` / adapter `.mjs`/`.py` originals
are left in place for reference (stagehandAdapter.mjs is still the one
actually executed) but are no longer the entrypoint — use
`bakeoff/run_bakeoff.py` from here on.

## Status

| Tool | Adapter status | Confidence |
|---|---|---|
| **mock** (not a real tool) | Working — returns the hand-authored example-vendor fixture. Proves the harness itself. Ported to `mock_adapter.py`, in-process. Verified 2026-09-23: full run (`--tools mock`) captures, validates, converts via a **live Stage 2 OpenAI call**, and passes **both** Stage 3 Playwright audits (primary + overfit-check booking). | N/A |
| **browser-use** | Implemented against verified v0.13.10 API (source-checked). `OPENAI_API_KEY` is live. Still needs `pip install browser-use==0.13.10` in `bakeoff/.venv` and a real vendor before a first run. Subprocess invocation unchanged; only the orchestrator calling it moved to Python. | High on the API shape; some `action_history()` dict keys are inferred (see adapter docstring) and may need a one-line fix on first real run. |
| **Stagehand** | Implemented against verified v4.1.0 API (docs-checked). Runs fully local, no Browserbase account needed. `OPENAI_API_KEY` live. Still needs `npm install @browserbasehq/stagehand` and a real vendor before a first run. `stagehandAdapter.mjs` is unmodified; `resolve_runtime.py`'s `node22_binary()` confirmed live 2026-09-23 (resolves to the already-installed nvm Node v24.20.0). | High — `observe()`/`act()` return a selector directly, the cleanest fit of the group. |
| **Skyvern** | Ported to `skyvern_adapter.py` (in-process, `httpx.AsyncClient`), all verified endpoint paths/headers preserved exactly from the original. API key live-verified (2026-09-23: `GET /api/v1/tasks` returns `[]` against the real cloud API — the API is mounted under `/api/v1`, not the bare `/v1` the source research implied). Not yet run for a real capture — needs a vendor. If the vendor needs login, a pre-created `SKYVERN_CREDENTIAL_ID` is also required. | High on auth/routing (live-verified); medium on the action-shape parsing (`skyvern_element_data`'s exact keys are still a best-effort guess — see adapter docstring). Per-step DOM/screenshot artifact fetching wasn't implemented this pass. |
| **Firecrawl** ("Prometheus") | Ported to `firecrawl_adapter.py` (in-process, `httpx.AsyncClient`). API key live (2026-09-23). Not yet run for a real capture. | **Low on the `/interact` capture logic specifically** — unchanged from the original assessment; see adapter docstring. If this one fails, that's itself a useful, expected finding. |
| **claude-devtools** (NEW — Claude + Chrome DevTools MCP) | Genuinely new adapter (`claude_devtools_adapter.py`), not a port. Drives the official `chrome-devtools-mcp@1.10.1` server via the `mcp` Python SDK, with Claude running a tool-use loop. **Live-verified against the real MCP server (2026-09-23, no API key needed for this part):** `list_tools()` returns the expected tool set; nearly every tool (incl. `navigate_page`, `take_snapshot`, `evaluate_script`) requires a `pageId` arg, with `pageId: 1` already open by default (`list_pages` → `"1: about:blank [selected]"`); `evaluate_script`'s `function` arg must be an actual JS function declaration; its result comes back as `"Script ran on page and returned:\n\`\`\`json\n{...}\n\`\`\`"` (not bare JSON) — the adapter's `_extract_json` parses that exact shape. The step-correlation logic (mirroring browserUseAdapter.py's fields_by_value/remaining_fields approach) and JSON extraction were unit-verified against that real observed response shape, and a claude-devtools-shaped funnel was confirmed to pass `app.pipeline.capture.validate_funnel_recording`. **NOT yet run end-to-end**: `ANTHROPIC_API_KEY` is empty in this worktree's `.env` (verified byte-for-byte — `OPENAI_API_KEY`/`SKYVERN_API_KEY`/`FIRECRAWL_API_KEY` are all populated, only this one is blank), so the actual Claude-driven tool-use loop against a live vendor has not been exercised. `--tools claude-devtools` currently (correctly) reports `not_configured`. Once a real key is set, run `.venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json --tools claude-devtools` to complete this. | Medium-high on protocol mechanics (live-verified against the real MCP server); the Claude-driven decision-making itself (does it reliably follow the evaluate_script-only instruction, correlate fields correctly across a real multi-turn run) is unverified pending a key. |

Every adapter is real code, not a placeholder. Run
`.venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json --tools mock`
any time to prove the harness itself still works with zero external
dependencies beyond the already-live `OPENAI_API_KEY` Stage 2 uses.

## Why Firecrawl is the weak link (findings, not assumption)

The plan flagged Firecrawl/"Prometheus" as unresearched and told us not to
substitute it into Stage 1 without evaluating it first. That evaluation:
Firecrawl was built for scraping/extraction; it's only recently grown an
action-taking capability (`/interact`, launched ~March 2026), and even that
hands you a bare remote-controlled browser rather than a purpose-built
recorder — you're back to writing your own Playwright-equivalent code and
capturing selectors yourself, which is exactly what the other three tools
already do natively. It's not disqualified outright (see its adapter), but
it starts from behind.

## How to actually run a comparison

```bash
# Harness smoke-test — no keys needed beyond the already-live OPENAI_API_KEY
# Stage 2 uses, proves the whole pipeline (capture -> validate -> convert -> audit x2):
.venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json --tools mock

# Once you have a key for one tool:
export OPENAI_API_KEY=...
.venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json --tools browser-use

# All five tools at once (default when --tools is omitted):
.venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json --tools browser-use,stagehand,skyvern,firecrawl,claude-devtools
```

Always invoke with the MAIN `.venv/bin/python` (never `bakeoff/.venv`,
which is browser-use-only — see `bakeoff/lib/resolve_runtime.py`). The
orchestrator resolves `bakeoff/.venv`'s interpreter itself for the one
adapter that needs it.

**Each adapter that has its key set makes REAL, BILLED API calls** the
moment you include it in `--tools` — there is no dry-run mode. Only include
a tool once you're ready to spend on it. Adapters without their key set
report `not_configured` immediately and cost nothing.

Results print as a table and are also written to
`bakeoff/results/<vendorId>-<timestamp>.json` (gitignored — these are run
artifacts, not source).

## Scoring

`bakeoff/lib/scorecard.py` runs each captured funnel recording through:

1. `app/pipeline/capture.py`'s `validate_funnel_recording` — does the output
   even match our schema?
2. `app/pipeline/convert.py`'s `convert_funnel_to_script` — the real Stage 2
   conversion (live LLM if configured, the offline mock generator otherwise —
   see the parent README).
3. `app/pipeline/audit.py`'s `audit_script` — against the primary sample
   booking, then a second, different booking (the overfit check).

A tool "wins" a vendor if it validates, converts, and passes both audits.
Ties (more than one tool passes) break on capture time, selector quality
(does it need our XPath→CSS attribute workaround, like browser-use/Stagehand
do, or return CSS directly, like Stagehand/claude-devtools?), and cost — see
the parent conversation's comparison criteria (login/multi-step handling,
replay determinism, self-hosting/data exposure).

## Running against a real vendor instead of the offline fixture

1. Create `bakeoff/vendors/<vendor>.bakeoff.json` — same shape as
   `example-vendor.bakeoff.json`, but a real `portalUrl`, no
   `startLocalFixtureServer`, and `fields` describing the vendor's actual
   form (label text + type — values are resolved from the sample booking at
   run time, not hardcoded).
2. If the vendor needs login, put test/sandbox credentials in a local JSON
   file (**never commit it**) and pass `--credentials <path>`.
3. Run the bake-off. **Every adapter here is instructed to stop before any
   real payment** (see `lib/task_spec.py`'s safety rule, identical in spirit
   to the main pipeline's Stage 3 audit gate) — but these are live AI agents
   driving a real page with a real session, which carries more inherent risk
   than the deterministic, code-reviewed scripts the main pipeline ships.
   Prefer a vendor sandbox/staging environment if one exists, per the plan's
   §7 safety constraint, before running any of this against production.
