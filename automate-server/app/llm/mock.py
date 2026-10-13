"""Deterministic stand-in for the live LLM call, used whenever no key is
configured for the selected provider. Ports src/openai/mock.js exactly. It is
NOT a placeholder string — for "convert" it template-generates a real,
working HeadoutAutomation script (as JS source text) from the funnel
recording, so the rest of the pipeline (audit gate, shipping, self-heal) is
provable end to end without a live key. Swap to a real model by setting
OPENAI_API_KEY / ANTHROPIC_API_KEY; the prompt/contract in
app/pipeline/convert.py does not change.

NOTE: this module generates JavaScript SOURCE TEXT from Python string
templates — the f-strings/concatenation below build up literal `.js` code,
not Python logic. Match the JS original's generated output character for
character, since a self-heal system elsewhere regex-matches the exact
`findEl(candidates, stepIndex, action)` 3-argument call shape this produces.
"""
import json
import re
from typing import Any, Optional


def mock_complete(purpose: str, user_content: Any) -> str:
    payload = extract_json_payload(user_content)
    if purpose == "convert":
        return "```js\n" + generate_script_from_funnel(payload.get("funnel")) + "\n```"
    if purpose == "drift-check":
        return json.dumps(diagnose_drift(payload))
    if purpose == "repair":
        # Stage 2.5 (app/pipeline/repair.py) calls convert.repair_funnel_script() with
        # the failing script + audit error + DOM attached as plain (non-JSON-fenced)
        # text alongside the usual booking-schema/funnel JSON blocks -- the offline
        # stand-in doesn't need to reason about WHY the prior script failed, it just
        # needs to hand back a script that passes the audit gate, the same guarantee
        # "convert" already provides, so repair-loop tests stay provable without a
        # live key.
        return "```js\n" + generate_script_from_funnel(payload.get("funnel")) + "\n```"
    raise ValueError(f"Unknown mock purpose: {purpose}")


def extract_json_payload(user_content: Any) -> dict:
    """The "convert" prompt embeds TWO ```json fenced blocks (booking schema,
    then the funnel recording) — merge every block's top-level keys rather
    than grabbing just the first, or `payload["funnel"]` silently comes back
    missing and the generated script ends up with zero steps."""
    if isinstance(user_content, list):
        text = ""
        for c in user_content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text") or ""
                break
    else:
        text = "" if user_content is None else str(user_content)

    blocks = [m.group(1) for m in re.finditer(r"```json\n([\s\S]*?)```", text)]
    if not blocks:
        bare = re.search(r"\{[\s\S]*\}", text)
        if bare:
            blocks.append(bare.group(0))

    merged: dict = {}
    for block in blocks:
        try:
            parsed = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            merged.update(parsed)
    return merged


def _json_str(value: Any) -> str:
    """JSON.stringify-equivalent for embedding into generated JS source: no
    extra whitespace (JS's default is compact), non-ASCII left as-is (JS
    doesn't \\u-escape non-ASCII by default either)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def read_path_expr(var_name: str, dotted_path: str) -> str:
    """Read a dotted path (e.g. "guestDetails.firstName") out of an object at
    runtime, inlined as JS source."""
    parts = [_json_str(p) for p in dotted_path.split(".")]
    acc = var_name
    for p in parts:
        acc = f"({acc} || {{}})[{p}]"
    return acc


def generate_script_from_funnel(funnel: Optional[dict]) -> str:
    steps = (funnel or {}).get("steps") or []
    lines = [generate_step_code(step, i) for i, step in enumerate(steps)]
    joined_lines = "\n".join(lines)
    return (
        "(function () {\n"
        "  function findEl(candidates, stepIndex, action) {\n"
        "    for (const sel of candidates) {\n"
        "      let el = null;\n"
        "      if (sel.startsWith(\"xpath=\")) {\n"
        "        el = document.evaluate(sel.slice(6), document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;\n"
        "      } else {\n"
        "        el = document.querySelector(sel);\n"
        "      }\n"
        "      if (el) return el;\n"
        "    }\n"
        "    throw new Error(`Step ${stepIndex} (${action}): no element matched candidates ${JSON.stringify(candidates)}`);\n"
        "  }\n"
        "  function setValue(el, value) {\n"
        "    const proto = Object.getPrototypeOf(el);\n"
        "    const setter = Object.getOwnPropertyDescriptor(proto, \"value\")?.set;\n"
        "    if (setter) setter.call(el, value); else el.value = value;\n"
        "    el.dispatchEvent(new Event(\"input\", { bubbles: true }));\n"
        "    el.dispatchEvent(new Event(\"change\", { bubbles: true }));\n"
        "  }\n"
        "\n"
        "  window.HeadoutAutomation = {\n"
        "    async run(booking, ctx) {\n"
        "      const credentials = (ctx && ctx.credentials) || {};\n"
        "      try {\n"
        f"{joined_lines}\n"
        "        return { status: \"PAYMENT_REQUIRED\" };\n"
        "      } catch (e) {\n"
        "        return { status: \"ERROR\", error: String((e && e.message) || e) };\n"
        "      }\n"
        "    },\n"
        "  };\n"
        "})();"
    )


def generate_step_code(step: dict, i: int) -> str:
    var_name = f"el{i}"
    candidates_literal = _json_str(step.get("selector_candidates") or [])
    action = step.get("action")
    # findEl throws its own error now (stepIndex + action baked in) — no separate
    # hand-written throw, so the candidates array exists in exactly one place per
    # step (see convert.py's CONTRACT §9: a second throw would duplicate it, and a
    # self-heal patch that only rewrites the findEl(...) call would leave that
    # second copy stale).
    find_line = f"        const {var_name} = findEl({candidates_literal}, {i}, {_json_str(action)});"

    if action == "fill":
        if step.get("requires_credentials"):
            cred_key = step.get("credential_key") or "username"
            source = f"credentials[{_json_str(cred_key)}]"
        else:
            source = read_path_expr("booking", step.get("field_to_booking_key") or "")
        return f"{find_line}\n        setValue({var_name}, {source});"
    if action in ("click", "submit"):
        return f"{find_line}\n        {var_name}.click();"
    # "navigate"/"wait" steps are a no-op in a single-injection replay; the
    # extension's executeWithNavigationRetry handles re-injection across a real
    # page navigation (see lib/autofill.js).
    return f"        // step {i}: {action} (handled by navigation retry, no DOM action)"


def diagnose_drift(payload: dict) -> dict:
    """Real diagnosis (with a real model) also reasons over screenshots. The
    offline stand-in tries a stable-attribute relocation first: if one of the
    failing selectors was a `[name="..."]` attribute selector and that name
    is still present on the current page, the field just got a new id — find
    it by its unchanged name rather than guessing the first selector on the
    page (which would misfire onto an unrelated element)."""
    reference_dom = payload.get("referenceDom") or ""
    current_dom = payload.get("currentDom") or ""
    failing_step_index = payload.get("failingStepIndex")
    failing_selectors = payload.get("failingSelectors") or []

    name_selector = next((sel for sel in failing_selectors if re.match(r'^\[name="[^"]+"\]$', sel)), None)
    if name_selector:
        name_value = re.search(r'\[name="([^"]+)"\]', name_selector).group(1)
        current_tag = find_tag_with_attr(current_dom, "name", name_value)
        new_id = get_attr(current_tag, "id") if current_tag else None
        if new_id:
            return {
                "stepIndex": failing_step_index,
                "diagnosis": f'Field\'s id changed but name="{name_value}" is unchanged; relocated via the stable name attribute.',
                "suggestedSelectors": [f"#{new_id}", name_selector],
            }

    # Next, try relocating by the field's visible <label> text — stable even when BOTH
    # id and name changed (a fuller vendor redesign), and the kind of anchor a real
    # model would also reason from alongside a screenshot.
    id_selector = next((sel for sel in failing_selectors if re.match(r'^#[\w-]+$', sel, flags=re.ASCII)), None)
    if id_selector:
        old_id = id_selector[1:]
        ref_field = next((f for f in extract_labelled_fields(reference_dom) if f["id"] == old_id), None)
        match = None
        if ref_field and ref_field.get("labelText"):
            match = next(
                (f for f in extract_labelled_fields(current_dom) if f["labelText"] == ref_field["labelText"]),
                None,
            )
        if match and match.get("id"):
            name_suffix = f' (name="{match["name"]}")' if match.get("name") else ""
            return {
                "stepIndex": failing_step_index,
                "diagnosis": (
                    f'Field relocated: label "{ref_field["labelText"]}" now points to '
                    f'#{match["id"]}{name_suffix}, was #{old_id}.'
                ),
                "suggestedSelectors": [
                    s for s in [f'#{match["id"]}', f'[name="{match["name"]}"]' if match.get("name") else None] if s
                ],
            }

    # Fallback: a structural id diff, only used when no stable attribute/label anchor matched.
    ref_selectors = extract_candidate_selectors(reference_dom)
    missing_ids = [sel for sel in ref_selectors if sel.startswith("#") and sel[1:] not in current_dom]
    return {
        "stepIndex": failing_step_index,
        "diagnosis": (
            f"Selector(s) no longer present on the page: {', '.join(missing_ids)}"
            if missing_ids
            else "No structural drift detected from DOM diff; likely a timing or navigation change."
        ),
        "suggestedSelectors": [],
    }


def extract_labelled_fields(dom: str) -> list:
    """Extracts { labelText, id, name } for each `<label>TEXT <input .../></label>`-style field."""
    fields = []
    for m in re.finditer(r"<label>([^<]*)<input\b([^>]*)>", dom, flags=re.IGNORECASE):
        fields.append({
            "labelText": m.group(1).strip(),
            "id": get_attr(m.group(2), "id"),
            "name": get_attr(m.group(2), "name"),
        })
    return fields


def extract_candidate_selectors(dom: str) -> list:
    ids = [f"#{m.group(1)}" for m in re.finditer(r'id="([\w-]+)"', dom, flags=re.ASCII)]
    names = [f'[name="{m.group(1)}"]' for m in re.finditer(r'name="([\w-]+)"', dom, flags=re.ASCII)]
    seen: list = []
    for sel in ids + names:
        if sel not in seen:
            seen.append(sel)
    return seen


def find_tag_with_attr(dom: str, attr: str, value: str) -> Optional[str]:
    tags = re.findall(r"<(?:input|select|textarea|button)\b[^>]*>", dom, flags=re.IGNORECASE)
    pattern = re.compile(f'{attr}="{value}"')
    for tag in tags:
        if pattern.search(tag):
            return tag
    return None


def get_attr(tag: Optional[str], attr: str) -> Optional[str]:
    if tag is None:
        return None
    m = re.search(f'{attr}="([^"]*)"', tag)
    return m.group(1) if m else None
