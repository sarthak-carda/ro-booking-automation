"""
Two calling conventions for the five capture-tool adapters, deliberately:

1. Subprocess adapters (browser-use, stagehand, claude-devtools) — each has a
   reason it can't live in-process with the (now Python) orchestrator:
     - browser-use needs `bakeoff/.venv`, a dependency-isolated environment
       (see resolve_runtime.py's docstring for the real incident that caused
       this) — it cannot be imported into the main venv's process.
     - stagehand is a Node-only library with no Python equivalent — it has to
       run as a Node subprocess (stagehandAdapter.mjs, unmodified).
     - claude-devtools launches its own `npx chrome-devtools-mcp` child
       process and drives a multi-turn Claude tool-use loop; running it as an
       isolated subprocess (via the MAIN venv's python3, since it needs
       `anthropic` + `mcp`) keeps its lifecycle/timeout handling identical to
       the other two rather than a special case inside the orchestrator.
   `run_subprocess_adapter()` below runs any of these: the adapter receives
   the task input as JSON on stdin, and MUST print exactly one JSON object as
   the LAST line of stdout:
     {"status": "ok", "funnel": {...}}
     {"status": "not_configured", "reason": "<what's missing and how to fix it>"}
     {"status": "error", "error": "<message>"}
   Anything else on stdout/stderr before that line is treated as log noise.
   This is the exact same protocol bakeoff/lib/runAdapter.js used, unchanged,
   so stagehandAdapter.mjs and the subprocess-calling half of
   browserUseAdapter.py needed zero modification.

2. In-process adapters (mock, skyvern, firecrawl) — now that the orchestrator
   itself is Python, these have no runtime conflict with the main venv, so
   they are plain `async def run(input: dict) -> dict` functions in
   bakeoff/adapters/*.py. Call them directly — no wrapper needed, no
   subprocess overhead, no JSON-over-stdio protocol to parse. See
   bakeoff/run_bakeoff.py for how each tool name maps to one calling
   convention or the other.
"""
import asyncio
import json
import time

# Real finding (2026-09-23, Prado): 300s wasn't enough for browser-use on a real
# multi-step funnel (language switch, calendar, pax-type fallback, skip-addons, guest
# form) -- it timed out with no result at all. Bumped to match Skyvern's own budget.
DEFAULT_TIMEOUT_S = 600


async def run_subprocess_adapter(
    tool: str,
    command: str,
    args: list[str] | None = None,
    input: dict | None = None,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Runs one capture-tool adapter as a subprocess and parses its result.

    Mirrors runAdapter.js's runAdapter() exactly, including that a result
    field returned by the adapter itself (e.g. its own "durationMs" if it
    ever set one) wins over our own bookkeeping value.
    """
    args = args or []
    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as err:
        return {
            "tool": tool,
            "status": "error",
            "error": f"Failed to spawn: {err}",
            "durationMs": int((time.monotonic() - start) * 1000),
        }

    payload = json.dumps(input).encode()

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "tool": tool,
            "status": "error",
            "error": f"Timed out after {timeout}s",
            "durationMs": int((time.monotonic() - start) * 1000),
        }

    duration_ms = int((time.monotonic() - start) * 1000)
    stderr_tail = stderr.decode(errors="replace")[-2000:]
    stdout_text = stdout.decode(errors="replace")

    lines = [line for line in stdout_text.strip().split("\n") if line.strip()]
    base = {"tool": tool, "durationMs": duration_ms, "stderrTail": stderr_tail}

    if not lines:
        return {**base, "status": "error", "error": "Adapter produced no stdout output"}

    last_line = lines[-1]
    try:
        result = json.loads(last_line)
    except json.JSONDecodeError as err:
        return {
            **base,
            "status": "error",
            "error": f"Adapter output was not valid JSON: {err}",
            "rawStdoutTail": stdout_text[-2000:],
        }

    return {**base, **result}
