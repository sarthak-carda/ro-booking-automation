#!/usr/bin/env python3
"""
CLI: runs one or more capture-tool adapters against one vendor, scores
each result through the real Stage 2/3 pipeline, and prints a comparison
table. See bakeoff/README.md for the full methodology.

Port of bakeoff/runBakeoff.js — the orchestrator itself is now Python (not
just some of the adapters), so this file replaces it entirely; runBakeoff.js
is no longer the entrypoint (see bakeoff/README.md for the updated status).

Usage:
  .venv/bin/python bakeoff/run_bakeoff.py --vendor bakeoff/vendors/example-vendor.bakeoff.json
  .venv/bin/python bakeoff/run_bakeoff.py --vendor <...> --tools browser-use,stagehand
  .venv/bin/python bakeoff/run_bakeoff.py --vendor <...> --tools mock   # harness smoke-test, no external tool/key needed

Each adapter that needs a real API key makes REAL, BILLED calls to that
tool's LLM/API once its key is present — this is not simulated. Only pass
--tools for the ones you actually want to spend on.

Five tools, two calling conventions (see bakeoff/lib/run_adapter.py's module
docstring for the full rationale): mock/skyvern/firecrawl run in-process as
plain `async def run(input) -> dict` functions; browser-use/stagehand/
claude-devtools run as subprocesses over the unchanged JSON-stdin/
one-JSON-line-stdout protocol, each needing its own interpreter —
bakeoff/.venv for browser-use (dependency isolation), a Node>=22 nvm install
for stagehand (Node-only library), and THIS SAME (main) venv for
claude-devtools (it needs `anthropic` + `mcp`, both only here).

Node.js on this machine is now used only for stagehandAdapter.mjs itself and
for launching `chrome-devtools-mcp` via `npx` inside claude_devtools_adapter.py
— never for orchestration, which is what moved to Python in this port.
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BAKEOFF_ROOT = REPO_ROOT / "bakeoff"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bakeoff.adapters import firecrawl_adapter, mock_adapter, skyvern_adapter  # noqa: E402
from bakeoff.lib.resolve_runtime import node22_binary, venv_python  # noqa: E402
from bakeoff.lib.run_adapter import run_subprocess_adapter  # noqa: E402
from bakeoff.lib.scorecard import print_score_table, score_capture_result  # noqa: E402
from bakeoff.lib.task_spec import build_adapter_input  # noqa: E402

try:
    from app.pipeline.context_builder import build_generation_request  # noqa: E402
except ImportError as _context_builder_import_err:  # pragma: no cover - fallback if this slice ever regresses
    def build_generation_request(vendor: dict, sample_booking: dict) -> dict:
        """Local fallback mirroring src/pipeline/contextBuilder.js, used only if
        app.pipeline.context_builder isn't importable (another agent's slice)."""
        if not vendor or not vendor.get("portalUrl"):
            raise ValueError("Vendor is missing portalUrl")
        if not sample_booking or not sample_booking.get("bookingId"):
            raise ValueError("Sample booking is missing bookingId")
        return {
            "vendorId": vendor.get("id"),
            "vendorName": vendor.get("name"),
            "portalUrl": vendor.get("portalUrl"),
            "requiresCredentials": bool(vendor.get("requiresCredentials")),
            "fulfillmentType": vendor.get("fulfillmentType") or sample_booking.get("fulfillmentType") or "unknown",
            "bookingSchemaSample": sample_booking,
        }

try:
    from tests_py.conftest import start_static_server  # noqa: E402
except ImportError:
    start_static_server = None

# In-process adapters (see bakeoff/lib/run_adapter.py's docstring for why these three
# don't need the subprocess protocol at all now that the orchestrator itself is Python).
IN_PROCESS_ADAPTERS = {
    "mock": mock_adapter.run,
    "skyvern": skyvern_adapter.run,
    "firecrawl": firecrawl_adapter.run,
}

# Subprocess adapters: `command` and `args` are functions, not values — resolved lazily,
# only for the tools actually selected via --tools, so a missing isolated runtime for one
# tool (see resolve_runtime.py for why these are isolated at all) never blocks the others.
SUBPROCESS_ADAPTERS = {
    "browser-use": {
        "command": venv_python,
        "args": lambda: [str(BAKEOFF_ROOT / "adapters" / "browserUseAdapter.py")],
    },
    "stagehand": {
        "command": node22_binary,
        "args": lambda: [str(BAKEOFF_ROOT / "adapters" / "stagehandAdapter.mjs")],
    },
    "claude-devtools": {
        # Runs under THIS interpreter (the main .venv) — needs `anthropic` + `mcp`,
        # neither of which live in bakeoff/.venv (that venv is browser-use-only).
        "command": lambda: sys.executable,
        "args": lambda: [str(BAKEOFF_ROOT / "adapters" / "claude_devtools_adapter.py")],
    },
}

ALL_TOOLS = ["mock", "browser-use", "stagehand", "skyvern", "firecrawl", "claude-devtools"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage-1 capture-tool bake-off harness")
    parser.add_argument("--vendor", required=True, help="Path to a *.bakeoff.json vendor config")
    parser.add_argument("--tools", default=",".join(ALL_TOOLS), help="Comma-separated tool list (default: all)")
    parser.add_argument("--credentials", dest="credentials_path", default=None, help="Path to a JSON credentials file")
    parser.add_argument(
        "--booking", dest="booking_path", default=None,
        help="Path to a real sample-booking JSON (default: fixtures/bookings/sample-booking-1.json)",
    )
    parser.add_argument(
        "--second-booking", dest="second_booking_path", default=None,
        help="Path to a second booking JSON for the overfit check (default: fixtures/bookings/sample-booking-2.json; pass the same value as --booking to skip a real second run)",
    )
    args = parser.parse_args(argv)
    args.tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    return args


async def run_one_tool(tool: str, adapter_input: dict) -> dict:
    if tool in IN_PROCESS_ADAPTERS:
        start = time.monotonic()
        try:
            result = await IN_PROCESS_ADAPTERS[tool](adapter_input)
        except Exception as err:  # noqa: BLE001 - an in-process adapter's own bug shouldn't kill the harness
            result = {"status": "error", "error": f"{type(err).__name__}: {err}"}
        result.setdefault("durationMs", int((time.monotonic() - start) * 1000))
        result.setdefault("tool", tool)
        return result

    adapter = SUBPROCESS_ADAPTERS[tool]
    try:
        command = adapter["command"]()
    except Exception as err:  # noqa: BLE001
        # e.g. bakeoff/.venv or the nvm Node 22 install isn't set up yet — a
        # setup gap, not a capture failure, so it's reported the same way.
        return {"tool": tool, "status": "not_configured", "reason": str(err)}

    return await run_subprocess_adapter(tool, command, adapter["args"](), adapter_input, cwd=str(REPO_ROOT))


async def main_async(argv: list[str]) -> None:
    args = parse_args(argv)

    vendor_config = json.loads(Path(args.vendor).read_text())
    credentials = json.loads(Path(args.credentials_path).read_text()) if args.credentials_path else None

    booking_path = Path(args.booking_path) if args.booking_path else (REPO_ROOT / "fixtures" / "bookings" / "sample-booking-1.json")
    second_booking_path = Path(args.second_booking_path) if args.second_booking_path else (REPO_ROOT / "fixtures" / "bookings" / "sample-booking-2.json")
    booking = json.loads(booking_path.read_text())
    second_booking = json.loads(second_booking_path.read_text())

    generation_request = build_generation_request(
        {
            "id": vendor_config["vendorId"],
            "name": vendor_config["vendorName"],
            "portalUrl": vendor_config["portalUrl"],
            "requiresCredentials": vendor_config.get("requiresCredentials"),
        },
        booking,
    )

    server = None
    if vendor_config.get("startLocalFixtureServer"):
        if start_static_server is None:
            print(
                "Warning: startLocalFixtureServer requested but no static-server helper is "
                "importable (tests_py/conftest.py not found) — skipping; the vendor's portalUrl "
                "must already be reachable on its own."
            )
        else:
            fixture = vendor_config["startLocalFixtureServer"]
            server = start_static_server(str(REPO_ROOT / fixture["dir"]), fixture["port"])
            print(f"Started local fixture server for offline capture at {vendor_config['portalUrl']}")

    try:
        adapter_input = build_adapter_input(vendor_config, credentials=credentials, booking=booking)
        rows = []

        for tool in args.tools:
            if tool not in ALL_TOOLS:
                print(f'Unknown tool "{tool}", skipping. Known: {", ".join(ALL_TOOLS)}')
                continue

            print(f"\nRunning {tool}...")
            result = await run_one_tool(tool, adapter_input)

            if result.get("status") == "not_configured":
                print(f"  [{tool}] not configured: {result.get('reason')}")
            elif result.get("status") == "error":
                print(f"  [{tool}] error: {result.get('error')}")
            else:
                step_count = len((result.get("funnel") or {}).get("steps") or [])
                print(f"  [{tool}] captured {step_count} steps in {result.get('durationMs')}ms")

            # Real crash found 2026-09-23 (TOSC/ticketone.it): audit_script lets a
            # navigation-level failure (e.g. net::ERR_HTTP2_PROTOCOL_ERROR, consistent
            # with a WAF/anti-bot block) propagate as an exception -- correct for the
            # main pipeline (run_pipeline.py's own try/except turns it into a "failed"
            # run), but here it was taking down the WHOLE bake-off, not just this one
            # tool's row. Scoring one tool's result must never crash the loop for the
            # rest of --tools.
            try:
                row = await score_capture_result(
                    tool=tool,
                    adapter_result=result,
                    generation_request=generation_request,
                    booking=booking,
                    second_booking=second_booking,
                    portal_url=vendor_config["portalUrl"],
                    credentials=credentials,
                )
            except Exception as err:  # noqa: BLE001 - one tool's scoring crash shouldn't kill the rest
                row = {
                    "tool": tool,
                    "captureStatus": result.get("status"),
                    "notes": [f"Scoring crashed: {type(err).__name__}: {err}"],
                }
                print(f"  [{tool}] scoring crashed: {type(err).__name__}: {err}")
            rows.append(row)

        print()
        print_score_table(rows)

        results_dir = BAKEOFF_ROOT / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out_file = results_dir / f"{vendor_config['vendorId']}-{int(time.time() * 1000)}.json"
        out_file.write_text(json.dumps({
            "vendor": vendor_config["vendorId"],
            "ranAt": datetime.now(timezone.utc).isoformat(),
            "rows": rows,
        }, indent=2))
        print(f"\nFull results written to {out_file.relative_to(REPO_ROOT)}")
    finally:
        if server:
            server.close()


def main() -> None:
    asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    main()
