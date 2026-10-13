#!/usr/bin/env python3
"""
Claude + Chrome DevTools MCP adapter — the fifth capture tool, and the only
one that's genuinely new work rather than a port (there was no Node original
for this one). Drives the official `chrome-devtools-mcp` server (launched via
`npx -y chrome-devtools-mcp@latest`, using the `mcp` Python SDK's stdio
client) with Claude running a tool-use loop as the "brain".

KNOWN GAP THIS ADAPTER WORKS AROUND: chrome-devtools-mcp's native `click`/
`fill` tools take a `uid` (a session-scoped id from `take_snapshot`'s a11y
tree) and confirm success only against that uid — never a resolvable CSS
selector. Once the session ends, that uid means nothing, which is useless
for a capture tool whose whole job is to hand off a durable, replayable
selector to Stage 2.

DESIGN DECISION (deliberate, not a workaround we'd rather not need): the
system prompt below instructs Claude to do ALL form-filling and clicking
through `evaluate_script` instead of the native `click`/`fill` tools — i.e.
Claude writes and runs its own small JS snippet per action, which both
performs the action AND computes+returns the exact CSS selector it used.
`take_snapshot` is used only for situational awareness (what fields exist,
how are they labeled), never as the action mechanism. This gives us a REAL,
execution-verified CSS selector straight from each tool call's own result —
no XPath-to-CSS guesswork like the browser-use/Stagehand adapters need.

VERIFIED LIVE (2026-09-23, against `chrome-devtools-mcp@1.10.1` via
`npx -y chrome-devtools-mcp@latest --headless=true --isolated=true`), since
none of this was in training data:
  - Nearly every tool (including `navigate_page`, `take_snapshot`,
    `evaluate_script`) REQUIRES a `pageId` argument. One page is already open
    by default before any tool is called — `list_pages` returns
    "1: about:blank [selected]" — so `pageId: 1` is used throughout unless a
    new page is explicitly opened. The system prompt tells Claude this
    directly rather than spending a turn discovering it.
  - `evaluate_script`'s `function` param must be an actual JS function
    declaration (its own schema example: `() => document.title`), not a bare
    statement list — the system prompt's example snippet is wrapped as
    `() => { ...; return {...}; }` accordingly.
  - Its result comes back as a single TextContent block shaped exactly like:
    "Script ran on page and returned:\\n```json\\n{...}\\n```" — NOT bare
    JSON. `_extract_json` below parses that fenced-code-block shape first,
    with a couple of fallbacks in case a future server version changes it
    slightly (e.g. a script that throws, or returns non-JSON-serializable
    output).

Protocol: reads adapter input as JSON on stdin, same shape as the other
subprocess adapters (browserUseAdapter.py / stagehandAdapter.mjs), and
prints exactly one JSON line to stdout as its last line — see
bakeoff/lib/run_adapter.py's docstring for why this one stays a subprocess
(it needs `anthropic` + `mcp`, both only in the MAIN venv, not
bakeoff/.venv, but is still kept process-isolated for lifecycle/timeout
handling identical to browser-use/stagehand).
"""
import asyncio
import json
import os
import re
import sys

# Real finding (2026-09-23, Prado, 3 runs): step counts varied wildly (0/2/8) run to
# run, and one run hit this cap with no final response at all -- Prado's real flow
# (custom calendar, pax-type fallback, multi-page checkout) needs more headroom than
# a simple fixed-field form does. Bumped from 30.
MAX_TURNS = 50
MAX_TOKENS = 4096


def emit(obj: dict) -> None:
    print(json.dumps(obj))
    sys.stdout.flush()


def debug(msg: str) -> None:
    if os.environ.get("BAKEOFF_DUMP_RAW"):
        print(msg, file=sys.stderr)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str):
    """Pulls a JSON object out of an evaluate_script tool result.

    Real observed shape (live-verified): "Script ran on page and returned:\\n
    ```json\\n{...}\\n```". Tries that fenced form first, then a bare `{...}`
    substring, then a whole-text parse, in that order — so a minor server
    version change to the wrapper text doesn't silently break correlation.
    """
    if not text:
        return None
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    brace_match = _BRACE_RE.search(text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def mcp_tool_to_anthropic_schema(tool) -> dict:
    """MCP tool schemas are already JSON Schema — maps over directly."""
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


def mcp_result_to_text(call_result) -> str:
    parts = []
    for block in getattr(call_result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    if parts:
        return "\n".join(parts)
    structured = getattr(call_result, "structured_content", None)
    return json.dumps(structured) if structured else ""


EVALUATE_SCRIPT_INSTRUCTIONS = """
SELECTOR CAPTURE RULE — for EVERY field fill and the final button click, do
NOT use the native `click` or `fill` tools. Use `evaluate_script` instead:
write a small JS function that finds the element, performs the action, and
returns a small JSON object describing what it did and the exact CSS
selector it used. `evaluate_script`'s `function` argument must be an actual
JS function declaration (e.g. an arrow function), not bare statements.

The selector MUST NEVER be null. Real checkout pages very often use custom
widgets (calendar cells, styled dropdowns, pax pickers) with no id or name
attribute — id/name alone is NOT enough. Compute the selector using this
exact priority order, falling through until one actually identifies the
element uniquely, and use whichever one works — never give up and return null:
  1. `#${el.id}` if el.id is non-empty.
  2. `[name="${el.name}"]` if el.name is non-empty.
  3. `[data-testid="..."]` / `[aria-label="..."]` / `[placeholder="..."]` if the
     element (or a close ancestor for a non-input element like a calendar cell
     or custom dropdown option) has one of these.
  4. A stable class-based selector: `tagName.class1.class2` using EVERY class on
     the element (not just the first), scoped further with a parent selector if
     the class alone could match multiple elements — BUT SKIP any class that
     looks STATE-DEPENDENT (`selected`, `active`, `current`, `today`,
     `highlighted`, `hover`, `focus`) when the element is a calendar/date cell.
     Real, repeated finding: a class like `.calendar-day.selected` describes
     WHICHEVER day happens to be selected/current at the moment of capture, not
     the specific date — replayed later (a different day), it silently selects
     the wrong date or matches nothing. For a date cell, skip straight to
     priority 5 instead if no non-state class/attribute exists.
  5. For a date/calendar cell with no stable attribute: an XPath matching the
     cell's own TEXT content (the day-of-month number), e.g.
     `xpath=//td[not(contains(@class,"disabled"))]//*[text()="15"]` — this is
     the PREFERRED answer for a calendar cell, not a last resort, since it's
     the one thing about that cell that will always be true for that date.
  6. Absolute last resort — nth-of-type scoped to a stable ancestor, e.g.
     '.time-slot-list > button:nth-of-type(3)'. This still counts as a real
     answer; a computed guess beats returning null.
Test your own selector inside the SAME evaluate_script call before returning
it — call document.querySelector(yourSelector) (or document.evaluate for an
xpath= selector) and confirm it resolves to the exact element you just acted
on. If it doesn't, revise it before returning.

ALSO return `domSnapshot`: the outerHTML of a nearby container (the closest
`table`/`ul`/`[role=grid]`/`[role=listbox]` ancestor, or just the element's
parent if none of those match), truncated to ~4000 characters. This gives
Stage 2 (script generation) the REAL markup to reason from instead of
guessing at generic widget conventions — this is what actually made the
state-dependent-class problem above fixable in practice, not just in theory.

Example for filling a field (adapt the lookup + selector fallback to the actual page):
() => {
  const el = document.querySelector('#first-name')
    || [...document.querySelectorAll('label')].find(l => l.textContent.includes('First name'))?.querySelector('input');
  el.value = 'Jamie';
  el.dispatchEvent(new Event('input', { bubbles: true }));
  let selector;
  if (el.id) selector = '#' + el.id;
  else if (el.name) selector = '[name="' + el.name + '"]';
  else if (el.className) selector = el.tagName.toLowerCase() + '.' + [...el.classList].join('.');
  else selector = 'input:nth-of-type(1)'; // adapt to a real scoped fallback for the actual element
  const container = el.closest('table, ul, [role="grid"], [role="listbox"]') || el.parentElement || el;
  return { selector, tagName: el.tagName, domSnapshot: container.outerHTML.slice(0, 4000) };
}

Example for a custom calendar-day click with no id/name at all (this is the
common case on real sites — do not skip straight to null, and do NOT use a
"selected"/"active"/"current"/"today" class as the selector — see priority 4-5
above):
() => {
  const day = [...document.querySelectorAll('td')].find(d => d.textContent.trim() === '15' && !d.className.includes('disabled'));
  day.click();
  const selector = 'xpath=//td[not(contains(@class,"disabled"))][text()="15"]';
  const container = day.closest('table, ul, [role="grid"]') || day.parentElement || day;
  return { selector, tagName: day.tagName, domSnapshot: container.outerHTML.slice(0, 4000) };
}

Always return that JSON object as the function's return value — the selector
you report becomes the permanent, execution-verified record of what you
actually did, since the native click/fill tools only confirm against an
ephemeral uid that means nothing once this session ends.

Use `take_snapshot` only to see what fields exist and how they're labeled —
never as the action mechanism itself.

PAGE ID: nearly every tool call (including navigate_page, take_snapshot,
evaluate_script) requires a `pageId` argument. One page is already open by
default before you call anything — use `pageId: 1` throughout unless you
deliberately open a new page yourself.
""".strip()


def build_system_prompt(input_data: dict) -> str:
    return "\n\n".join([
        input_data["taskPrompt"],
        EVALUATE_SCRIPT_INSTRUCTIONS,
        "Start with `navigate_page` (pageId: 1) to the portal URL. Use `take_snapshot` to see the "
        "page. For every field fill and the final button click, use `evaluate_script` as instructed "
        "above, never the native click/fill tools, so each action's result tells us the real "
        "selector used. Once you've reached the point described in the task (or the final action is "
        "done), reply with a short plain-text summary and stop calling tools.",
    ])


def _record_step_from_evaluate_script(
    steps: list[dict],
    tool_input: dict,
    result_text: str,
    input_data: dict,
    fields_by_value: dict,
    remaining_fields: list[dict],
    credential_keys_used: list[str],
) -> None:
    """Mirrors browserUseAdapter.py's fields_by_value/remaining_fields correlation: matches an
    evaluate_script call back to a known field by looking for that field's literal value inside the
    JS source Claude sent (the most reliable signal we have, short of parsing the JS AST), falling
    back to consuming fields in the order the task prompt listed them."""
    parsed = _extract_json(result_text)
    selector = parsed.get("selector") if isinstance(parsed, dict) else None

    function_src = str((tool_input or {}).get("function") or "")
    is_click = ".click(" in function_src or (isinstance(parsed, dict) and str(parsed.get("action", "")).lower() == "click")

    field_key = None
    requires_credentials = False
    credential_key = None

    if not is_click:
        matched = None
        for value, field in fields_by_value.items():
            # Same false-positive risk as stagehandAdapter.mjs's correlateField (found
            # 2026-09-23): a short numeric value like "2" matches almost any JS source as a
            # bare substring. Require it to not be embedded in a longer digit run at least.
            if not value:
                continue
            if value.isdigit() and len(value) < 4:
                if not re.search(rf"(?<!\d){re.escape(value)}(?!\d)", function_src):
                    continue
            elif value not in function_src:
                continue
            matched = field
            break
        if matched:
            field_key = matched["fieldKey"]
            remaining_fields[:] = [f for f in remaining_fields if f is not matched]
        elif input_data.get("requiresCredentials") and input_data.get("credentials"):
            creds = input_data["credentials"]
            username = creds.get("username")
            password = creds.get("password")
            if (username and username in function_src) or (password and password in function_src):
                requires_credentials = True
                credential_key = "username" if "username" not in credential_keys_used else "password"
                credential_keys_used.append(credential_key)
        elif remaining_fields:
            field_key = remaining_fields.pop(0)["fieldKey"]

    dom_snapshot = parsed.get("domSnapshot") if isinstance(parsed, dict) else None

    steps.append({
        "index": len(steps),
        "action": "click" if is_click else "fill",
        "url": input_data["portalUrl"],
        "selector_candidates": [selector] if selector else [],
        "dom_snapshot": dom_snapshot,
        "screenshot": None,
        "field_to_booking_key": field_key,
        "requires_credentials": requires_credentials,
        "credential_key": credential_key,
    })


async def run_capture(input_data: dict, anthropic_api_key: str) -> dict:
    from anthropic import AsyncAnthropic
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    model = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5"
    client = AsyncAnthropic(api_key=anthropic_api_key)
    server_params = StdioServerParameters(command="npx", args=["-y", "chrome-devtools-mcp@latest"])

    steps: list[dict] = []
    fields_by_value = {str(f["value"]): f for f in input_data["fields"]}
    remaining_fields = list(input_data["fields"])
    credential_keys_used: list[str] = []
    system_prompt = build_system_prompt(input_data)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = [mcp_tool_to_anthropic_schema(t) for t in tools_result.tools]

            messages: list[dict] = [{"role": "user", "content": "Begin the task now."}]

            for turn in range(MAX_TURNS):
                response = await client.messages.create(
                    model=model,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=MAX_TOKENS,
                )
                debug(f"TURN {turn} stop_reason={response.stop_reason}")

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                messages.append({"role": "assistant", "content": response.content})

                if not tool_uses:
                    break  # Claude produced a final text response — done

                tool_result_blocks = []
                for tu in tool_uses:
                    try:
                        call_result = await session.call_tool(tu.name, tu.input or {})
                        result_text = mcp_result_to_text(call_result)
                        is_error = bool(getattr(call_result, "is_error", False))
                    except Exception as e:  # noqa: BLE001 - a single bad tool call shouldn't kill the run
                        result_text = f"Tool call failed: {type(e).__name__}: {e}"
                        is_error = True

                    if tu.name == "evaluate_script" and not is_error:
                        _record_step_from_evaluate_script(
                            steps, tu.input, result_text, input_data,
                            fields_by_value, remaining_fields, credential_keys_used,
                        )

                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                        "is_error": is_error,
                    })

                messages.append({"role": "user", "content": tool_result_blocks})
            else:
                debug(f"Hit MAX_TURNS ({MAX_TURNS}) without a final response — returning steps captured so far.")

    return {
        "vendorId": input_data["vendorId"],
        "portalUrl": input_data["portalUrl"],
        "capturedAt": None,
        "loginRequired": bool(input_data.get("requiresCredentials")),
        "steps": steps,
    }


def main() -> None:
    input_data = json.loads(sys.stdin.read())

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return emit({"status": "not_configured", "reason": "ANTHROPIC_API_KEY not set."})

    try:
        import anthropic  # noqa: F401
        import mcp  # noqa: F401
    except ImportError as e:
        return emit({
            "status": "not_configured",
            "reason": f"`anthropic`/`mcp` packages not importable: {e}. Both are already in the main "
                      "`.venv` per requirements.txt — run this adapter with `.venv/bin/python3`, not "
                      "bakeoff/.venv (that venv is isolated for browser-use only).",
        })

    try:
        funnel = asyncio.run(run_capture(input_data, api_key))
        emit({"status": "ok", "funnel": funnel})
    except Exception as e:  # noqa: BLE001 - mirrors the other adapters' catch-all
        emit({"status": "error", "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
