"""Stage 2 — Conversion.

Ports src/pipeline/convert.js. Prompts the model with the funnel recording +
the booking JSON shape + the `HeadoutAutomation.run(booking, ctx)` contract,
and asks for a script. This is the same prompt whether a live key is
configured or not — see app/llm/client.py for the offline fallback.

CONTRACT's items 1-9 are reproduced VERBATIM from the Node source (byte-for-byte,
extracted by evaluating the original JS template literal so no hand-decoding
of its nested backtick/${...} escaping could introduce a transcription
error) — it is safety-critical (payment-never, credential-never-logged, the
exact 3-argument `findEl(candidates, stepIndex, action)` contract a self-heal
system elsewhere depends on for regex-patching one step). Items 10-13 are a
Python-side addition (2026-10-06, Stage 2.5/app/pipeline/repair.py) ported
back from real findings while hand-building the Prado/TOSC vendor scripts —
NOT present in the original Node source. They extend the contract (multi-page
dispatch, idempotent repeatable steps, a settle-poll for transiently
non-actionable elements, and a `debug` sibling field on final failure) without
touching anything in 1-9, including the exact `findEl`/retry-loop shapes the
self-heal system and Stage 2.5's own narrow-patch path both depend on.
`repair_funnel_script` below (Stage 2.5's broad whole-script repair path)
reuses this SAME CONTRACT verbatim as its system prompt — never a
relaxed/forked variant — specifically so a repair pass can't drift away from
the payment-safety rules a fresh "fix this" prompt might be tempted to relax.
"""
import json
import re
from typing import Optional

from app.llm.client import complete

# Mirrors app/self_heal/drift_check.py's MAX_DOM_CHARS/_truncate_dom pattern (not imported
# directly -- that module's helper is underscore-private to its own narrow-patch use case,
# and a DOM snapshot attached to a repair prompt is cost/context-budget noise past this size
# either way, same reasoning as drift_check.py's own comment).
MAX_DOM_CHARS = 40_000


def _truncate_dom(dom: Optional[str]) -> Optional[str]:
    if not isinstance(dom, str) or len(dom) <= MAX_DOM_CHARS:
        return dom
    return dom[:MAX_DOM_CHARS] + f"\n<!-- truncated at {MAX_DOM_CHARS} chars -->"

CONTRACT = r"""You are generating a browser-injected JavaScript file for a Headout Revenue
Operations automation extension. The script MUST:

1. Register `window.HeadoutAutomation = { async run(booking, ctx) { ... } }`.
2. Return one of exactly these shapes (never throw out of run()):
   - { status: "PAYMENT_REQUIRED" }   // reached the vendor's payment page — STOP, never submit payment
   - { status: "CONFIRMED", reference } // only if a post-payment confirmation is read back
   - { status: "ERROR", error: "<message>" }
3. NEVER click a final "Pay" / "Place order" / "Confirm purchase" button that charges money.
   Reaching the payment page and stopping is success. If the funnel recording shows a step
   past a point of no return (auto-charge on the last click), do not generate a step for it —
   stop the script one step earlier and return PAYMENT_REQUIRED there instead. If you are
   NOT CERTAIN whether a given click would trigger a charge, treat it as if it does and stop
   one step earlier. A script that stops one step too soon is a trivial, cheap fix on the next
   pass; a script that submits a real payment is never an acceptable failure mode, so always
   resolve uncertainty in the direction of stopping too early.
4. If a login step is present, use `ctx.credentials.username` / `ctx.credentials.password` —
   NEVER hardcode a credential value into the script, and never write a credential value to
   `console.log` or into a returned error message, even for debugging.
5. Fill fields by reading the exact dotted path given in the funnel recording's
   `field_to_booking_key` out of the `booking` argument (which matches the sample
   booking JSON schema given below). If a funnel step has no `field_to_booking_key` (a
   constant selection, a checkbox that's always checked the same way, a fixed dropdown
   value), use the literal value captured in the recording rather than inventing a booking
   field to read. If a step requires solving a CAPTCHA or other bot-check, do not attempt to
   bypass or guess your way past it — stop there and return `{ status: "ERROR", error: "..." }`
   naming it, since that needs a human, not a retry.
6. Only act on ONE page load per invocation — do not attempt to handle a navigation
   yourself (a page reload/redirect between funnel steps is retried by the extension's
   own re-injection logic; your script only needs to be correct for a single document).
7. Write plain, readable JS. No external libraries, no eval, no template frameworks.
   A human reviews this file before it ships, so prefer clarity over cleverness.
8. Keep the legacy fallback in mind only insofar as not colliding with
   `window.HeadoutOrderFormAutofill` — do not define that global.
9. Locate every element through a local helper with EXACTLY this shape — it throws its OWN
   error, so the candidates array exists in exactly one place per step, never two, and it
   supports an `xpath=`-prefixed candidate for the text-matching case described below:
   `function findEl(candidates, stepIndex, action) { for (const sel of candidates) { let el = null; if (sel.startsWith("xpath=")) { el = document.evaluate(sel.slice(6), document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; } else { el = document.querySelector(sel); } if (el) return el; } throw new Error(\`Step \${stepIndex} (\${action}): no element matched candidates \${JSON.stringify(candidates)}\`); }`
   Call it once per funnel step as:
   `const el<N> = findEl([...selector candidates...], <N>, "<action>");`
   - `<N>` is EXACTLY the step's own index from the funnel recording (not a freshly counted
     sequence) — matching both the variable name `el<N>` and the numeric second argument.
     This is what lets a self-heal patch target the same step across regenerations.
   - `<action>` is a single lowercase word with no spaces (e.g. `click`, `fill`, `select`,
     `check`).
   - List candidates from most specific/stable (a `data-testid` or `id` attribute) to least
     specific (a bare tag/class), so a broad fallback is only used once every more specific
     candidate has already failed.
   - STATE-DEPENDENT CLASSES ARE NOT STABLE CANDIDATES (real, repeated finding — do not skip
     this). A calendar/date-picker day cell is very often only distinguishable by a CSS class
     that describes TRANSIENT STATE relative to today's real-world date or the current
     selection — e.g. `ui-datepicker-current-day`, `.today`, `.selected`, `.active`,
     `.highlighted`. That class points to whatever day happens to be "current" or "selected"
     AT THE MOMENT THE PAGE HAPPENS TO LOAD, not to the specific date this booking actually
     needs — it will silently select the wrong day, or match nothing at all, days or weeks
     after the script was generated. If a funnel step's captured selector(s) look like this —
     or the step's `field_to_booking_key` is itself a date (e.g. `inventoryDate`) — do NOT use
     that state-dependent class as (or as the only) candidate. Instead, add an `xpath=`
     candidate that matches on the cell's own STABLE, unchanging content: the visible
     day-of-month number, computed from the actual booking value at runtime. For example, for
     a step fill/click whose value is a date like `2026-10-15`, extract the day number (`15`)
     from the booking field and emit something like:
     `xpath=//td[not(contains(@class,"disabled")) and not(contains(@class,"other-month"))]//a[text()="` + new Date(bookingValue).getDate() + `"]`
     (adapt the exact XPath to the funnel recording's actual markup, but keep the principle:
     compute the target day-of-month from the booking value inside the script, don't hardcode
     the day number that happened to be captured — a script generated in September and run in
     November must still select the 15th of whatever month the booking is actually for). Put
     this computed xpath candidate FIRST in the array when a step is date-related; a
     state-dependent class, if you include it at all, belongs only as a last-resort fallback.
     IMPORTANT — when a step's `dom_snapshot` field is present (real captured markup around
     that element, not just the bare selector strings), READ IT and base the XPath's tag names,
     attribute names, and class names on what's ACTUALLY there, not on a generic guess at what
     a typical calendar/dropdown widget looks like (e.g. don't assume jQuery-UI's exact class
     names unless `dom_snapshot` itself shows that markup) — a selector built from real,
     provided markup is far more likely to survive contact with the real page than one built
     from a plausible-sounding assumption. When `dom_snapshot` is null/absent for a step, you
     have no choice but to reason generically from the selector strings alone — that's fine,
     just don't treat a generic guess as equally reliable when real markup was available and
     ignored.
   - CALENDAR MONTH/YEAR NAVIGATION (real, repeated finding — do not skip this). A date-picker's
     day cells almost always belong to whichever MONTH is currently displayed — the number "15"
     appears in every month, so an XPath that matches only on the day-of-month (as described
     above) can silently select the WRONG month's 15th, or match nothing at all (if that day is
     disabled/in the past for the currently-shown month), whenever the calendar isn't already
     showing the booking's target month at the moment the script runs. This is likely: the
     script can run on a real-world date far from both when it was generated AND from the
     booking's own date, so the widget's default/opening month will often NOT be the target
     month. Check the step's `dom_snapshot` for a month/year marker on the day cells or their
     container (e.g. a `data-month`/`data-year` attribute, jQuery-UI's typical shape, or a
     visible "September 2026"-style header) — if present, do BOTH of the following:
       (a) Add the target month/year into the day cell's own XPath predicate wherever the
           markup supports it, e.g.
           `xpath=//td[@data-month="` + targetMonth0Indexed + `" and @data-year="` + targetYear + `"]/a[text()="` + targetDay + `"]`
           computing `targetMonth0Indexed`/`targetYear`/`targetDay` from the booking value at
           runtime via `new Date(bookingValue).getMonth()` / `.getFullYear()` / `.getDate()`
           (note `getMonth()` is 0-indexed, matching jQuery-UI's own `data-month` convention —
           do not confuse that with a 1-indexed display label).
       (b) Immediately before that `findEl` call, add a small bounded navigation loop: while a
           cell matching the target month/year isn't yet present in the DOM, click the
           calendar's own "next month" control (find its real selector from the funnel
           recording's own captured steps or `dom_snapshot` — e.g. a `.ui-datepicker-next`-style
           class; use a "previous" control the same way if the target month could be earlier
           than what's shown), re-checking after each click, capped at a sane bound (e.g. 24
           iterations, covering two years) so a genuinely-missing month fails fast instead of
           looping forever. REAL, CONFIRMED FINDING: a jQuery-UI-style datepicker can re-render
           its day grid in an OBSERVABLE two-phase way — the visible day numbers can paint
           before the `data-month`/`data-year`/`data-handler` attributes finish binding onto
           those cells, so checking the probe selector immediately (with no settle time at all,
           not even right after a click) can catch the DOM mid-render and wrongly conclude the
           target isn't there yet, clicking "next" past the correct month even though it was
           already showing (confirmed live on a real vendor: the exact same query for the same
           state returned 0 matches once and 28 matches moments later, no click in between).
           Guard against this with a short settle-wait BEFORE the very first check too, not just
           after each click:
           ```js
           await new Promise((r) => setTimeout(r, 500));
           for (let i = 0; i < 24 && !document.querySelector(monthYearProbeSelector); i++) {
             const nextBtn = document.querySelector(NEXT_MONTH_SELECTOR);
             if (!nextBtn) break;
             nextBtn.click();
             await new Promise((r) => setTimeout(r, 500));
           }
           ```
           (adapt `monthYearProbeSelector`/`NEXT_MONTH_SELECTOR` to the real markup; this loop is
           navigation, not the step's own `findEl` call, so it does not change the "one
           `findEl([...], <N>, action)` call per step, verbatim" rule below — it runs BEFORE
           that call, inside the same step's code, not in place of it).
     If `dom_snapshot` shows no month/year marker at all, you have no way to verify or navigate
     months — fall back to the day-of-month-only approach above, but note in a comment that this
     step may drift if run in a different month than it was captured.
   - Do NOT add a separate `if (!el<N>) throw ...` after this call. `findEl` already throws
     when nothing matches — a second, hand-written throw would duplicate the candidates array
     as a second, independent copy of the string, which a later self-heal patch (which only
     replaces the array passed into `findEl`) would leave stale and wrong.
   - The line `const el<N> = findEl([...], <N>, "<action>");` must appear VERBATIM — no
     `await`, no wrapping it in a helper (e.g. a `waitForEl(...)`/`pollFor(...)`-style
     function), no assigning its result through an intermediate promise. A self-heal pass
     locates a step to patch by literally regex-matching this exact call shape; wrapping it
     in anything else means that step can never be auto-patched again, only escalated to a
     human. See the timing note below for where polling/retry logic actually belongs instead.
   - COOKIE-CONSENT / GDPR BANNER STEPS ARE OPTIONAL, NOT MANDATORY (real, repeated finding —
     these banners are genuinely non-deterministic: geo-targeting, A/B tests, and session state
     mean the exact same vendor page sometimes shows one and sometimes doesn't, confirmed via
     back-to-back live runs against the same real site). If a step's candidates look like a
     cookie-consent/GDPR/privacy banner's own accept button (common patterns: an id/class
     containing `onetrust`, `cookie`, `consent`, `gdpr`, `cky-`, or similar, especially as the
     FIRST step in the funnel with no `field_to_booking_key`), do NOT let a missing banner fail
     the whole script. Keep the `const el<N> = findEl([...], <N>, "<action>");` call verbatim as
     always, but wrap ONLY that step's own execution (the findEl call plus its click) in a
     try/catch that silently continues past a "no element matched" error for THIS step
     specifically, rather than letting it propagate up and trigger the outer retry loop (which
     would otherwise burn its whole ~4s budget retrying a step that will never succeed because
     there was never a banner to dismiss in the first place):
     ```js
     try {
       const el<N> = findEl([...], <N>, "click");
       el<N>.click();
     } catch (e) {
       // Cookie/consent banner not present this run -- optional step, not a failure.
     }
     ```
     This keeps `const el<N> = findEl([...], <N>, "click");` intact and self-heal-patchable
     (the regex a patch locates it by doesn't care what wraps the line, only that the line
     itself matches) — nothing later in the script needs to reference `el<N>` for a banner
     dismissal, so scoping it inside the try block costs nothing. Every OTHER step keeps the
     plain, non-try/catch form described above; this exception is specifically for a
     banner-dismissal step and should not be applied to steps that represent a real, required
     part of the booking flow.
10. PAGE-AWARE DISPATCH FOR MULTI-PAGE FUNNELS (real, repeated finding — do not skip this).
    Each funnel step carries its own captured `url`. If every step's `url` has the SAME
    pathname, ignore this section — the single-`runSteps` shape in the timing note below is
    correct as-is. If steps span MORE THAN ONE distinct pathname (a real page navigation
    partway through the funnel, not just an async-loaded section of the same page), the
    single-`runSteps`-retried-from-the-top shape is WRONG: once a real navigation has
    happened, retrying "from the top" replays early steps (e.g. a calendar/date picker) on a
    later page that no longer has that element at all, and can never succeed. Instead, group
    the funnel's steps into contiguous sections by pathname (in capture order) and generate
    one function per section plus a dispatcher that checks `location.pathname` FIRST to
    determine which section's elements should even be on the page right now, then runs ONLY
    that section — never assume step order alone tells you what page you're on:
    ```js
    async function runSection0(booking, ctx) { /* steps whose captured url matched pathname A */ }
    async function runSection1(booking, ctx) { /* steps whose captured url matched pathname B */ }

    async function runSteps(booking, ctx) {
      if (location.pathname === "<pathname A>") return runSection0(booking, ctx).then(() => runSection1(booking, ctx));
      if (location.pathname === "<pathname B>") return runSection1(booking, ctx);
      // adapt the exact pathname comparisons to what the funnel actually captured; the
      // principle is "pick the section matching the CURRENT page", not "assume section 0".
    }
    ```
    The outer retry loop described in the timing note below still wraps `runSteps` the same
    way — it just now retries "whichever section applies to the current page" instead of
    always the same fixed function, so a same-page retry never replays a page that's already
    behind you. Known boundary: this does nothing for a true single-page-app vendor whose URL
    never changes between funnel steps (state lives in the DOM, not the route) — if every
    step shares one pathname, this section doesn't apply, see above.
11. IDEMPOTENT REPEATABLE STEPS (real, repeated finding — do not skip this). If a funnel step
    represents an action that must be repeated a variable number of times to reach a target
    (a quantity/pax "+" stepper, not a single click), do NOT generate a blind fixed-count
    loop that always clicks N times from an assumed starting point of zero. The outer retry
    loop (timing note below) can re-run this step after it already made partial progress (a
    later step failed, the whole sequence retried) — blindly repeating the full count would
    overshoot the real target. Read the control's CURRENT displayed value from the DOM first,
    compute the remaining delta to the booking's target value, and only click that many times
    (zero if already at target):
    ```js
    const current = parseInt(document.getElementById(<qty element id>)?.value || "0", 10) || 0;
    for (let target = current + 1; target <= <booking target count>; target++) {
      const el = findEl([...], <N>, "click");
      el.click();
      // then wait for the displayed value to actually reach `target` before looping again
    }
    ```
12. SETTLE-POLL FOR A TRANSIENTLY NON-ACTIONABLE ELEMENT (real, repeated finding — do not
    skip this). This is DIFFERENT from "not yet rendered" (which the outer retry in the
    timing note below already handles by re-running `findEl` from scratch). Some controls are
    already IN the DOM and match their `findEl` candidates, but carry a temporary
    disabled/loading class or attribute while an async request is in flight (confirmed live:
    a "+" button can read `class="add blockeddate disabled"` for over a second after being
    clicked, while the page's own AJAX call updates a cart, then return to its normal enabled
    class once that resolves) — clicking immediately after `findEl` returns such an element
    can silently no-op or throw. When a funnel step's `dom_snapshot` shows this shape (a
    class/attribute that looks state-dependent in a way that implies "busy", not "wrong
    element" — e.g. containing `disabled`, `loading`, `busy`, `pending`), poll for the
    element to become actionable AFTER `findEl` returns it, before acting on it, bounded to a
    few seconds:
    ```js
    const el<N> = findEl([...], <N>, "click");
    const actionableDeadline = Date.now() + 5000;
    while (Date.now() < actionableDeadline && el<N>.matches(".disabled, [disabled], .loading")) {
      await new Promise((r) => setTimeout(r, 200));
    }
    el<N>.click();
    ```
    (adapt the `.matches(...)` selector to whatever the real captured markup shows as its
    "busy" state — don't guess a generic class name if `dom_snapshot` shows a specific one).
13. DEBUG CONTEXT ON FINAL FAILURE GOES IN A SIBLING FIELD, NEVER CONCATENATED INTO `error`
    (real, repeated finding — do not skip this, and read it carefully, it is easy to get
    wrong in a way that breaks OTHER systems silently). When the outer retry loop (timing
    note below) is about to give up and return `{ status: "ERROR", error: message }`, ALSO
    include a sibling `debug` key with cheap-to-gather diagnostic context — current
    `location.href`/`location.pathname`, and, if easily inspectable, the actual current
    class/state of whatever element the failing step was trying to interact with. This makes
    an otherwise-opaque failure diagnosable by an automated repair pass without a human
    needing to reproduce it live. CRITICAL: `error` itself MUST remain byte-identical to the
    exact "Step <N> (<action>): no element matched candidates [...]" shape produced by
    `findEl`'s own throw — do NOT append, prefix, or otherwise concatenate the debug
    information into the `error` string itself. A self-heal system elsewhere matches `error`
    against that exact shape with an anchored regex to auto-diagnose single-step drift;
    anything added into the string breaks that matching for every downstream consumer, not
    just this run. Debug context belongs ONLY in the separate `debug` key:
    ```js
    if (!isMissingElement || Date.now() >= deadline) {
      return {
        status: "ERROR",
        error: message, // byte-identical to findEl's own throw -- nothing appended
        debug: { url: location.href, pathname: location.pathname },
      };
    }
    ```

Note on timing: a field can be genuinely absent (real drift) or simply not yet rendered
(an async-loaded checkout step). Do not call `findEl` a single time and immediately throw —
but do NOT implement the retry by changing how `findEl` itself is called (see the rule above).
Instead, retry at the OUTER level: wrap the whole step sequence in a loop that catches a
"Step <N> (<action>): no element matched candidates" error, waits ~200ms, and re-runs the step
sequence from the top (each `findEl` call stays exactly as specified — a re-run just calls it
again), up to a total budget of ~4s, before finally returning that error as `{ status: "ERROR" }`.
Concretely:
```js
async function runSteps(booking, ctx) {
  // ... every step here as `const el<N> = findEl([...], <N>, "<action>");` followed by
  // whatever that step does (fill/click) — nothing about timing lives in this function.
}

async function run(booking, ctx) {
  const deadline = Date.now() + 4000;
  while (true) {
    try {
      return await runSteps(booking, ctx);
    } catch (e) {
      const message = e && e.message ? e.message : String(e);
      const isMissingElement = /^Step \d+ \(\w+\): no element matched candidates/.test(message);
      if (!isMissingElement || Date.now() >= deadline) {
        // See item 13 above: `error` stays byte-identical to `message`; `debug` is a
        // separate sibling key, never concatenated into the error string itself.
        return { status: "ERROR", error: message, debug: { url: location.href, pathname: location.pathname } };
      }
      await new Promise((r) => setTimeout(r, 200));
    }
  }
}
```
This retries the *whole* sequence rather than one field at a time, which is fine — funnel steps
before the missing one are idempotent to redo (re-filling a field with the same value is a
no-op in effect) and this keeps every `findEl` call site untouched and self-heal-patchable.

Respond with ONLY the JavaScript, in a single ```js fenced code block."""

_CODE_BLOCK_RE = re.compile(r"```(?:js|javascript)?[ \t]*\r?\n([\s\S]*?)```")


async def convert_funnel_to_script(generation_request: dict, funnel: dict) -> dict:
    """@param generation_request - output of context_builder.build_generation_request
    @param funnel - a validated funnel recording (see pipeline/capture.py)
    @returns {"script": str, "mocked": bool}
    """
    booking_schema_json = json.dumps(generation_request.get("bookingSchemaSample"), indent=2)
    funnel_json = json.dumps({"funnel": funnel}, indent=2)
    requires_credentials = generation_request.get("requiresCredentials")

    user_text = "\n".join([
        "## Booking JSON schema (sample, real shape from Aries):",
        "```json",
        booking_schema_json,
        "```",
        "",
        "## Funnel recording (ordered steps captured from the live vendor portal):",
        "```json",
        funnel_json,
        "```",
        "",
        f"## Vendor: {generation_request.get('vendorName')} ({generation_request.get('portalUrl')})",
        # JS template-literal string coercion of a boolean lowercases it ("true"/"false"),
        # unlike Python's default str(bool) ("True"/"False") -- match the JS output exactly.
        f"Login required: {'true' if requires_credentials else 'false'}",
    ])

    result = await complete(
        system=CONTRACT,
        user_content=[{"type": "text", "text": user_text}],
        purpose="convert",
    )

    return {"script": extract_code_block(result["text"]), "mocked": result["mocked"]}


async def repair_funnel_script(
    generation_request: dict, funnel: dict, script: str, error: str, dom: Optional[str] = None,
) -> dict:
    """Stage 2.5's (app/pipeline/repair.py) broad whole-script repair path — used when
    the narrow single-selector self-heal patch doesn't apply or didn't fix a failing
    audit. Deliberately reuses CONTRACT verbatim as the system prompt (see module
    docstring) and the same booking-schema/funnel framing as convert_funnel_to_script,
    with the failing script, the audit's error, and a DOM snapshot at the point of
    failure appended so the model can see what actually broke, not just regenerate
    blind. Caller is responsible for the audit (not this function) and for enforcing
    a real-attempt budget — this just asks the model for a corrected script once.

    @param generation_request - output of context_builder.build_generation_request
    @param funnel - a validated funnel recording (see pipeline/capture.py)
    @param script - the current, failing script source
    @param error - the audit's error message for that script
    @param dom - a DOM snapshot captured at the point of failure (audit.py's "dom"), if any
    @returns {"script": str, "mocked": bool}
    """
    booking_schema_json = json.dumps(generation_request.get("bookingSchemaSample"), indent=2)
    funnel_json = json.dumps({"funnel": funnel}, indent=2)
    requires_credentials = generation_request.get("requiresCredentials")

    user_text = "\n".join([
        "## Booking JSON schema (sample, real shape from Aries):",
        "```json",
        booking_schema_json,
        "```",
        "",
        "## Funnel recording (ordered steps captured from the live vendor portal):",
        "```json",
        funnel_json,
        "```",
        "",
        f"## Vendor: {generation_request.get('vendorName')} ({generation_request.get('portalUrl')})",
        f"Login required: {'true' if requires_credentials else 'false'}",
        "",
        "## This script already exists and FAILED a real audit run against the live vendor",
        "## site. Fix it -- do not just regenerate from scratch and discard what already",
        "## works; the failure below is specific, fix that specifically. The contract above",
        "## (including items 10-13) still applies in full, with no exceptions.",
        "",
        "### Current (failing) script:",
        "```js",
        script,
        "```",
        "",
        "### Audit error:",
        error,
    ])
    if dom:
        user_text += "\n\n### DOM snapshot at the point of failure:\n```html\n" + _truncate_dom(dom) + "\n```"

    result = await complete(
        system=CONTRACT,
        user_content=[{"type": "text", "text": user_text}],
        purpose="repair",
    )

    return {"script": extract_code_block(result["text"]), "mocked": result["mocked"]}


def extract_code_block(text: str) -> str:
    """Tolerate a missing language tag, extra whitespace, or CRLF after the
    opening fence -- a strict `\n` match silently falls through to `text`
    (which still has the backticks in it) if the model's formatting drifts
    even slightly."""
    match = _CODE_BLOCK_RE.search(text)
    return (match.group(1) if match else text).strip()
