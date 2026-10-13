#!/usr/bin/env node
/**
 * Stagehand adapter — implemented against @browserbasehq/stagehand v4,
 * verified against npm registry + current docs on 2026-09-22 (see
 * bakeoff/README.md). Runs entirely locally (no Browserbase account
 * needed — v4 made that optional) and drives the vendor portal.
 *
 * VERIFIED (from docs.stagehand.dev/v4 AND a real act() call against both
 * example.com and Prado, 2026-09-23 — the docs-derived shape below was WRONG
 * and was the root cause of every captured step coming back with zero
 * selector candidates for a full pass, see asArray()'s comment):
 *   - npm install @browserbasehq/stagehand@^4.1.0
 *   - const browser = await localBrowser.launch();
 *     const stagehand = await Stagehand.create({ browser, model: { modelName, apiKey } });
 *     const [page] = await browser.context.pages();
 *   - stagehand.act(instruction) returns
 *     { data: { success, message, actionDescription, actions: [{selector, description, method, arguments}] }, metadata }
 *     — NOT a bare top-level `actions` array as the docs examples suggest.
 *     `selector` observed as an absolute XPath string prefixed `xpath=`.
 *   - Credentials: act(instruction, { variables: { username, password } }) with
 *     %username%/%password% placeholders in the instruction text — the docs
 *     state these are substituted locally and never sent to the LLM provider.
 *
 * SELECTOR FORMAT CAVEAT (real finding): Stagehand's `selector` can be an
 * XPath OR a CSS string depending on the page — our generated scripts locate
 * elements via `document.querySelector()` (see pipeline/convert.js's
 * contract), which does not understand XPath. Since we have a live Playwright
 * page here (Stagehand v4 wraps CDP/Playwright locally), we resolve whatever
 * selector Stagehand gives us back into a Playwright locator and read the
 * element's actual id/name/class/placeholder attributes off the live DOM,
 * building proper CSS candidates from those — the same approach as the
 * browser-use adapter, for a fair comparison between the two.
 */

// How many times to re-issue the SAME custom instruction before moving on -- see the
// "Real gap found 2026-09-23 (Prado)" comment below for why more than 1 is needed.
//
// Real gap found 2026-09-24 (Prado): a cap of 3 was fine for a "reveal widget, then
// select" instruction (2 physical actions), but a "fill in the customer details form"
// instruction covers MANY fields (first/last name, email, address, city, postal, country
// -- up to 9 for some vendors) and act() only performs one field per call, same as the
// calendar case. With a cap of 3, only ~3 fields ever got filled, the real "Continue"
// button stayed disabled (missing required fields), and the next instruction's act() call
// found nothing usable to click, producing an unresolvable step.
//
// Real gap found 2026-09-24, round 2 (user feedback, Prado + midaticket.com): raising
// this to a single generous GLOBAL constant fixed the multi-field case but caused the
// opposite problem everywhere else -- a plain "accept the cookie banner" or "select the
// time slot" instruction kept getting re-issued well past the one click it needed, and
// the model doesn't reliably recognize "I'm already done" on its own, so it would wander
// (re-opening a settings icon, re-toggling a selection) instead of just stopping. Since
// this session's own per-field/per-action instruction splitting means most instructions
// are now genuinely single-shot, the DEFAULT is much lower than before; an instruction
// that legitimately needs more (a multi-field form, a stepper needing several clicks for
// a booking's pax count) opts into a higher one explicitly via
// {text: "...", maxAttempts: N} instead of every instruction paying for the rare case.
const DEFAULT_MAX_ACT_ATTEMPTS_PER_INSTRUCTION = 2;

async function main() {
  const input = JSON.parse(await readStdin());

  const apiKey = process.env.OPENAI_API_KEY ?? process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return emit({ status: "not_configured", reason: "OPENAI_API_KEY or ANTHROPIC_API_KEY not set — Stagehand needs an LLM for act()/observe()." });
  }

  let Stagehand, localBrowser;
  try {
    ({ Stagehand, localBrowser } = await import("@browserbasehq/stagehand"));
  } catch {
    return emit({ status: "not_configured", reason: "@browserbasehq/stagehand is not installed. Run: npm install @browserbasehq/stagehand@^4.1.0" });
  }

  let browser;
  try {
    browser = await localBrowser.launch();
    const stagehand = await Stagehand.create({
      browser,
      model: { modelName: process.env.STAGEHAND_MODEL ?? "openai/gpt-4.1", apiKey },
    });
    const [page] = await browser.context.pages();
    await page.goto(input.portalUrl, { waitUntil: "domcontentloaded" });

    const steps = [];

    if (input.requiresCredentials && input.credentials) {
      const loginActions = await stagehand.act(
        "Log in using %username% for the username/email field and %password% for the password field, then submit.",
        { variables: { username: input.credentials.username, password: input.credentials.password } }
      );
      for (const a of asArray(loginActions)) {
        steps.push(await buildStep(page, steps.length, input.portalUrl, a, { requiresCredentials: true }));
      }
    }

    if (input.customInstructions) {
      // Real gap found 2026-09-23 (Prado): this adapter used to ONLY ever iterate
      // input.fields as flat "type X into field Y" acts, completely ignoring
      // customInstructions/taskPrompt -- so a vendor with real branching logic
      // (pax-type fallback, "skip add-ons", calendar/time-slot selection) got
      // treated as a meaningless flat field list with no funnel structure at all.
      // act() has no memory between calls, so each step gets the full field-values
      // block inlined so it can resolve "the Date value given below" on its own.
      const valuesBlock = Object.entries(input.fieldValues || {})
        .map(([label, value]) => `  - ${label}: "${value}"`)
        .join("\n");
      const safetyReminder =
        "Never click a button that finalizes/confirms a real payment or charge, and never enter payment/card details, even if this step mentions paying. Stop before any such action.";

      for (const [instructionIndex, rawInstruction] of input.customInstructions.entries()) {
        // Real gap found 2026-09-24 (user feedback, Prado + midaticket.com): a single
        // GLOBAL retry budget (originally 15) applied uniformly to every instruction was
        // exactly why a plain "accept the cookie banner once" or "select the date" kept
        // wandering for several extra attempts instead of stopping after one -- that
        // budget exists for the rare instruction that genuinely needs several physical
        // actions (reveal a widget, then select from it), not for a simple one-shot
        // click. Vendor configs can now give an instruction as {text, maxAttempts}
        // instead of a bare string to say explicitly how many attempts it should ever
        // get; a bare string keeps a conservative default that's much lower than the old
        // global constant, since most instructions (after this session's per-field/
        // per-action splitting) are genuinely single-shot.
        const instruction = typeof rawInstruction === "string" ? rawInstruction : rawInstruction.text;
        const maxAttempts =
          typeof rawInstruction === "object" && Number.isFinite(rawInstruction.maxAttempts)
            ? rawInstruction.maxAttempts
            : DEFAULT_MAX_ACT_ATTEMPTS_PER_INSTRUCTION;
        const combined = `Booking details you may need for this step:\n${valuesBlock}\n\nStep: ${instruction}\n\n${safetyReminder}`;

        // Real gap found 2026-09-23 (Prado): a single instruction often needs MORE than
        // one physical interaction to actually complete -- e.g. "select the date" first
        // requires opening a hidden date-picker widget, THEN clicking the target day.
        // act() only returns actions for the ONE call it was given, so a single-shot
        // approach silently drops the earlier "reveal" step from the captured funnel
        // entirely (confirmed live: the calendar table didn't exist in the DOM at all
        // until some prior interaction opened it, but no "open calendar" step was ever
        // recorded). Re-issue the SAME instruction up to MAX_ACT_ATTEMPTS times -- if the
        // first call only opened a widget, the second call (now that it's open) proceeds
        // to the actual selection, and both physical actions get recorded as real steps.
        // Real gap found 2026-09-24 (Prado): tried two different early-stop heuristics
        // here (stop the moment any action set repeats anywhere in this instruction's
        // history; stop when a before/after DOM-length fingerprint is unchanged) and
        // BOTH made things worse than the plain consecutive-repeat check -- the fixed
        // "seen anywhere" version quit before a legitimately-oscillating time-slot
        // picker ever finished converging, and the DOM-fingerprint version raced a real
        // SPA's async re-render (captured "after" before the page had actually updated)
        // and quit almost immediately on nearly every instruction, including ones that
        // WERE making progress. Reverted to the simple, previously-working check: stop
        // only when the exact same action set repeats on two CONSECUTIVE attempts. Each
        // instruction gets its OWN fresh attempt budget (this loop resets every
        // iteration of the outer `for (const instruction of ...)`), so a stuck
        // instruction burning its full cap does NOT reduce a later instruction's budget
        // -- confirmed by re-reading run 1's log, where instruction 5 (guest-details
        // form) got 0 actions on its very own fresh attempt 0, not because it ran out of
        // turns but because the page it landed on genuinely had no form on it yet (see
        // the page-state/screenshot probe below).
        let lastActionKey = null;
        // Real gap found 2026-09-24 (midaticket.com): a "same action repeated" check that
        // only compares against the IMMEDIATELY PRIOR attempt misses a 2-cycle oscillation
        // (A, B, A, B, ...) -- confirmed live on a "dismiss the cookie banner if visible"
        // instruction that kept alternating between the actual accept button and an
        // unrelated "Consent Preferences" icon, burning its full attempt budget without
        // ever being caught, since no two CONSECUTIVE attempts were ever identical. Track a
        // short two-step window instead of just the last step.
        let secondLastActionKey = null;
        let stepsForThisInstruction = 0;
        let navClickCount = 0;
        for (let attempt = 0; attempt < maxAttempts; attempt++) {
          const actions = await stagehand.act(combined);
          const resolvedActions = asArray(actions);
          console.error(
            `[stagehand] instruction ${instructionIndex} ("${instruction.slice(0, 60)}") attempt ${attempt}: ` +
              `${resolvedActions.length} action(s) -- ${resolvedActions.map((a) => `${a.method}@${String(a.selector).slice(0, 60)}`).join(", ") || "(none)"}`
          );
          if (resolvedActions.length === 0) {
            // Real gap found 2026-09-24 (Prado): a zero-action first attempt on a
            // multi-field "fill the form" instruction usually means the form isn't on
            // the current page at all (an intermediate review/continue step was missed
            // upstream), not that the instruction is simply done -- dump page state AND
            // a screenshot so a human can tell those two cases apart on sight instead of
            // guessing from a bare "0 actions" line.
            if (attempt === 0) {
              try {
                const pageState = await page.evaluate(() => ({ url: location.href, title: document.title }));
                console.error(`[stagehand] instruction ${instructionIndex}: 0 actions on first attempt -- current page: ${pageState.url} ("${pageState.title}")`);
              } catch {
                // page state probe is best-effort diagnostics only
              }
              if (process.env.BAKEOFF_DEBUG_SCREENSHOT_DIR) {
                try {
                  const shotPath = `${process.env.BAKEOFF_DEBUG_SCREENSHOT_DIR}/instruction-${instructionIndex}-zero-actions.png`;
                  await page.screenshot({ path: shotPath });
                  console.error(`[stagehand] instruction ${instructionIndex}: saved debug screenshot to ${shotPath}`);
                } catch (err) {
                  console.error(`[stagehand] instruction ${instructionIndex}: screenshot failed:`, err?.message ?? err);
                }
              }
            }
            break; // nothing left to do for this instruction
          }

          const actionKey = JSON.stringify(resolvedActions.map((a) => a.selector));
          const isRepeat = actionKey === lastActionKey || actionKey === secondLastActionKey;
          secondLastActionKey = lastActionKey;
          lastActionKey = actionKey;

          // Real gap found 2026-09-24 (midaticket.com): a quantity-stepper "+"/"-" button
          // has the SAME selector on every click (it doesn't move or change class based on
          // count), so stopping on "same action repeated" -- correct for a toggle that
          // would otherwise get stuck -- was also stopping a genuinely cumulative stepper
          // after just ONE click, undercounting pax. Build the step(s) first so we can tell
          // a stepper click apart from a real stuck-repeat by the clicked element's own
          // text (only knowable after resolution), and only stop the instruction when the
          // repeat wasn't a stepper click.
          let stopInstruction = false;
          for (const a of resolvedActions) {
            const step = await buildStep(page, steps.length, input.portalUrl, a, { fields: input.fields });
            const isStepperClick = step.action === "click" && isRepeatableClickText(step._elementText);
            const isNavClick = step.action === "click" && isNavClickText(step._elementText);
            // _elementText is kept on the pushed step (stripped only right before emit) so
            // cleanSteps() downstream can make the same stepper-vs-toggle distinction --
            // otherwise its own consecutive-run collapsing would undo this fix by keeping
            // just the last of several genuinely cumulative "+" clicks.
            if (isRepeat && !isStepperClick) {
              console.error(`[stagehand] instruction ${instructionIndex} attempt ${attempt}: same action repeated -- not making progress, stopping`);
              stopInstruction = true;
              break;
            }
            if (isNavClick) {
              navClickCount += 1;
              if (navClickCount > MAX_NAV_CLICKS) {
                console.error(`[stagehand] instruction ${instructionIndex} attempt ${attempt}: hit MAX_NAV_CLICKS (${MAX_NAV_CLICKS}) on a "${step._elementText}" control -- stopping to avoid overshooting`);
                stopInstruction = true;
                break;
              }
            }
            // Real gap found 2026-09-24 (Prado): a step with zero resolved selector
            // candidates (buildStep couldn't find the element Stagehand claims it acted
            // on -- e.g. a stale/disabled element, or a click that was a no-op) fails
            // funnel schema validation AND is useless to Stage 2 (no candidates to work
            // with). Recording it just to preserve a 1:1 step count with act() calls is
            // worse than dropping it -- a self-heal-able script needs real candidates,
            // not a placeholder step that can never be found on replay either.
            if (step.selector_candidates.length === 0) {
              console.error(`[stagehand] instruction ${instructionIndex} attempt ${attempt}: dropping unresolvable step (no selector candidates)`);
              continue;
            }
            steps.push(step);
            stepsForThisInstruction += 1;
          }
          if (stopInstruction) break;
        }
        console.error(`[stagehand] instruction ${instructionIndex} done: ${stepsForThisInstruction} step(s) recorded`);
      }
    } else {
      for (const field of input.fields) {
        const actions = await stagehand.act(`Type exactly "${field.value}" into the field labeled "${field.label}"`);
        for (const a of asArray(actions)) {
          steps.push(await buildStep(page, steps.length, input.portalUrl, a, { fieldKey: field.fieldKey }));
        }
      }

      const finalActions = await stagehand.act(input.finalAction?.description ?? "proceed to the next step");
      for (const a of asArray(finalActions)) {
        steps.push(await buildStep(page, steps.length, input.portalUrl, a, {}));
      }
    }

    await browser.close();

    emit({
      status: "ok",
      funnel: {
        vendorId: input.vendorId,
        portalUrl: input.portalUrl,
        capturedAt: null,
        loginRequired: Boolean(input.requiresCredentials),
        steps: cleanSteps(steps),
      },
    });
  } catch (err) {
    if (browser) await browser.close().catch(() => {});
    emit({ status: "error", error: `${err.name ?? "Error"}: ${err.message ?? err}` });
  }
}

/**
 * Real shape (confirmed live, 2026-09-23, against a real act() call on both example.com
 * and Prado — this was the root cause of every step coming back with zero selector
 * candidates): { data: { success, message, actionDescription, actions: [{selector,
 * description, method, arguments}] }, metadata: {...} }. Docs examples showing a bare
 * top-level `actions` array don't match the actual SDK response at this version.
 */
function asArray(actions) {
  if (Array.isArray(actions)) return actions;
  if (Array.isArray(actions?.data?.actions)) return actions.data.actions;
  if (Array.isArray(actions?.actions)) return actions.actions;
  return actions ? [actions] : [];
}

/** For custom-instructions mode: match this action back to a known field by checking
 * whether that field's literal value appears anywhere in the action Stagehand returned
 * (description/arguments) -- same value-matching idea as the other adapters' correlation
 * logic, since a single free-text instruction doesn't tell us up front which field (if any)
 * it will touch.
 *
 * Real bug found 2026-09-23 (TOSC/midaticket), tried twice: a short value like pax count "2"
 * matches as a bare substring almost anywhere (an id, a class like "col-2", an unrelated
 * count) -- every single captured step came back mislabeled as the pax field, even with a
 * word-boundary check (CSS classes commonly embed small numbers as suffixes, e.g. "col-2",
 * which still satisfies a naive word-boundary test). A short numeric value just doesn't carry
 * enough information to correlate reliably against arbitrary page markup -- for those, no
 * match (null) is the honest answer, not a confident wrong guess. Longer/non-numeric values
 * (names, emails, dates) keep the plain substring check, where false positives are far less
 * likely to begin with. */
/** Real gap found 2026-09-24 (Prado): an AI-agent capture tool EXPLORES the live page --
 * it doesn't execute a known-good recipe -- so the raw step list can contain exploration
 * noise that should never be replayed literally: a toggle clicked twice in a row (e.g. the
 * language switcher opened then immediately reverted), and clicks that landed on an element
 * whose ONLY distinguishing feature is a generic ".disabled"/"disabledLink" class (almost
 * certainly a near-miss on a slot that wasn't actually selectable, captured while the agent
 * was still figuring out which time slot to pick, not a real interaction worth repeating).
 * Confirmed this was the actual cause of a real Stage 3 audit failure ("Script kept
 * navigating without completing") -- the generated script reproduced this noise
 * instruction-for-instruction, and replaying it against a fresh page never converged the
 * same way live exploration eventually did. Cleaned up before emitting: drop steps whose
 * only candidate is a bare disabled-class selector, and collapse an immediate run of
 * identical (action, candidates) steps down to just the last one (the one whose outcome the
 * agent actually built on). Re-indexes afterward since findEl's stepIndex must stay
 * contiguous with the step's position (see convert.py's CONTRACT §9). */
/** Real gap found 2026-09-24 (midaticket.com): a "+"/"-" quantity stepper needs this same
 * repeatable-click treatment as a symbol-labeled button, but a calendar's month-navigation
 * "Next"/"Previous" control is WORD-labeled (its own text is literally "Next", not a bare
 * "+"/"-"/chevron) and needed the exact same fix -- clicking it twice in a row (same selector
 * both times, since the control itself doesn't move or change) is genuinely cumulative
 * (advance two months), not a stuck repeat. Match on the element's own text, not the
 * candidate selector, since that's the only signal available after resolution. */
function isRepeatableClickText(text) {
  return /^([+\-−–]|next|previous|prev|increase|increment|decrease|decrement)$/i.test((text || "").trim());
}

/** Real gap found 2026-09-24 (midaticket.com), second round: exempting a repeatable click
 * from the same-action stop entirely removed the only thing bounding it -- with nothing else
 * to signal "you've arrived," act() kept clicking "Next" for the full MAX_ACT_ATTEMPTS_PER_INSTRUCTION
 * budget (15 clicks = ~15 months forward) instead of the 1 click actually needed, silently
 * landing on the wrong month and breaking every step after it. A quantity stepper ("+"/"-")
 * doesn't have this problem the same way -- the booking's own pax count is a small, known
 * bound already implicitly capped by MAX_ACT_ATTEMPTS_PER_INSTRUCTION. Calendar/list
 * navigation ("next"/"previous") has no such natural small bound from the instruction alone,
 * so give it a much tighter cap of its own -- a booking date more than a few months out is
 * rare enough that requiring a more specific instruction is better than silently overshooting. */
const MAX_NAV_CLICKS = 4;
function isNavClickText(text) {
  return /^(next|previous|prev)$/i.test((text || "").trim());
}

/** A dropdown/menu-opener toggle returns to its ORIGINAL (closed) state after an even
 * number of identical clicks -- unlike re-selecting the same calendar day or list option
 * twice, which is simply idempotent (ends up in the one state either way). Heuristic: guess
 * "toggle-like" from common naming conventions on the trigger element itself. */
function isToggleLikeSelector(candidates) {
  const joined = (candidates || []).join(" ").toLowerCase();
  return /trigger|switcher|toggle|dropdown|accordion|collapse|expand/.test(joined);
}

function cleanSteps(steps) {
  const isBareDisabled = (step) => {
    const candidates = step.selector_candidates || [];
    return candidates.length === 1 && /^\.(disabled|disabledLink)$/.test(candidates[0]);
  };
  const filtered = steps.filter((s) => !isBareDisabled(s));

  // Group consecutive steps sharing the identical (action, candidates) into runs, then decide
  // per run how to collapse it.
  const runs = [];
  for (const step of filtered) {
    const last = runs[runs.length - 1];
    if (
      last &&
      last[0].action === step.action &&
      JSON.stringify(last[0].selector_candidates) === JSON.stringify(step.selector_candidates)
    ) {
      last.push(step);
    } else {
      runs.push([step]);
    }
  }

  // Real gap found 2026-09-24 (Prado): the first version of this cleanup always collapsed a
  // run of identical consecutive steps down to just the LAST one. That's correct for
  // re-selecting the same calendar day/list option twice (idempotent either way), but wrong
  // for a genuine open-then-close TOGGLE pair (e.g. the language-switcher trigger clicked
  // twice) -- "keep just the last" there means "keep just the OPEN click", which left the
  // dropdown open on replay and covered/blocked the calendar underneath it, breaking the very
  // next step (confirmed via a live Stage 3 audit failure). A toggle-like trigger clicked an
  // EVEN number of times is a net no-op (ends back where it started) -- drop the whole run. An
  // ODD count ends up toggled once -- keep exactly one. Anything not toggle-like keeps the
  // previous "last of the run" behavior.
  const collapsed = [];
  // Real gap found 2026-09-24 (midaticket.com): a quantity-stepper "+"/"-" click is neither
  // a toggle (each click has a lasting, CUMULATIVE effect, not a cancel-the-previous-click
  // effect) nor idempotent (unlike re-picking the same calendar day, clicking "+" twice is
  // not the same as clicking it once) -- collapsing a run of these down to "the last one"
  // would silently undercount every quantity a capture run genuinely incremented more than
  // once. Identified by the clicked element's own text (captured in buildStep, stripped
  // below): keep every step in a stepper run, not just one.
  const isStepperRun = (run) => run[0].action === "click" && isRepeatableClickText(run[0]._elementText);

  for (const run of runs) {
    if (isStepperRun(run)) {
      collapsed.push(...run); // cumulative -- every click counts
    } else if (isToggleLikeSelector(run[0].selector_candidates)) {
      if (run.length % 2 === 0) continue; // net no-op -- drop the whole run
      collapsed.push(run[0]); // odd count -- ends up toggled once
    } else {
      collapsed.push(run[run.length - 1]); // idempotent selection -- keep the last
    }
  }

  return collapsed.map((s, i) => {
    const { _elementText, ...clean } = s;
    return { ...clean, index: i };
  });
}

function correlateField(action, fields) {
  if (!fields?.length) return null;
  const haystack = JSON.stringify(action);
  const match = fields.find((f) => {
    if (f.value == null) return false;
    const value = String(f.value);
    if (value.length === 0) return false;
    if (/^\d+$/.test(value) && value.length < 4) return false; // too short/generic to trust a substring match
    return haystack.includes(value);
  });
  return match ? match.fieldKey : null;
}

/** Resolves whatever selector Stagehand returned into real CSS-selector candidates via the live DOM. */
/** Extracts a short symbol/word fallback target (e.g. "+") plus disambiguating context
 * words (e.g. "Full price") from Stagehand's own free-text `description` of an action, for
 * the semantic-fallback resolution in buildStep below. Quoted substrings of length <= 3 are
 * treated as the fallback text itself (symbols/short labels like "+"/"-"/"OK"); longer quoted
 * substrings become disambiguators. Returns nulls when the description gives nothing short
 * enough to be a safe fallback target -- a long/generic string would risk matching the wrong
 * element on the page, which is worse than not falling back at all. */
function buildResolutionArgs(rawSelector, description) {
  const quoted = [...(description || "").matchAll(/'([^']{1,40})'/g)].map((m) => m[1]);
  // Real gap found 2026-09-24 (midaticket.com), second round: restricting fallbackText to
  // quotes of length <= 3 correctly picked out a "+" but missed a time-slot box whose own
  // display text is a longer quoted string (e.g. "9:30 AM - 7:00 PM") alongside an even
  // LONGER quoted section name ("Choose the time of the visit") -- that instruction's
  // description had no quote short enough to qualify at all, so no fallback was even
  // attempted. The SHORTEST quoted string is a better general heuristic across both shapes
  // observed so far: for "The small '+' button ... for 'Full price' ...", "+" (1 char) beats
  // "Full price" (10); for "... 'Choose the time of the visit' section: '9:30 AM - 7:00 PM'",
  // the time is shorter than the section name. No arbitrary length cutoff.
  const fallbackText = quoted.length ? quoted.reduce((a, b) => (b.length < a.length ? b : a)) : null;
  const disambiguators = quoted.filter((q) => q !== fallbackText);
  return { sel: rawSelector, fallbackText, disambiguators };
}

async function buildStep(page, index, portalUrl, action, extra) {
  const method = String(action.method ?? "").toLowerCase();
  const isFill = method.includes("fill") || method.includes("type");
  const fieldKey = extra.fieldKey ?? correlateField(action, extra.fields);
  const rawSelector = action.selector;
  let candidates = [];
  const debug = (...args) => {
    if (process.env.BAKEOFF_DUMP_RAW) console.error("[stagehand]", ...args);
  };
  debug(`step ${index} rawSelector=`, JSON.stringify(rawSelector), "action=", JSON.stringify(action).slice(0, 300));

  // Real bug found 2026-09-23: `page.locator(sel).evaluate()` throws "locator.evaluate
  // is not a function" against Stagehand v4's `page` object here -- it's a partial
  // wrapper around CDP, not a full Playwright Page, and its Locator doesn't implement
  // evaluate(). page.evaluate() (a more fundamental method) does work, so resolve the
  // element with plain in-page JS instead (native document.evaluate() for XPath,
  // querySelector for CSS) rather than going through the Locator API at all.
  let domSnapshot = null;
  let resolvedText = null;
  const DOM_SNAPSHOT_MAX_CHARS = 4000;
  if (typeof rawSelector === "string" && rawSelector.length > 0) {
    try {
      const resolved = await page.evaluate((args) => {
        const { sel, fallbackText, disambiguators } = args;
        let el = null;
        if (sel.startsWith("xpath=")) {
          el = document.evaluate(sel.slice(6), document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        } else {
          el = document.querySelector(sel);
        }
        // Real gap found 2026-09-24 (midaticket.com), second round: the absolute XPath can
        // resolve to SOME element (not null) after a layout shift -- just the WRONG one, or
        // one with literally no identifying attributes at all (confirmed:
        // `<button class="" data-dir="up">`, no id/name and an EMPTY class) -- so gating the
        // fallback on `!el` alone let a uselessly-resolved primary match block it entirely.
        // Trigger the fallback whenever the primary match has nothing usable to build a
        // candidate from, not only when it's literally absent.
        const hasUsableIdentity = (e) =>
          !!e && (e.id || e.getAttribute?.("name") || (e.className && String(e.className).trim()) || e.getAttribute?.("data-dir") || e.getAttribute?.("aria-label"));
        // Real gap found 2026-09-24 (midaticket.com): Stagehand's XPath is ABSOLUTE
        // (position-indexed, e.g. ".../div[3]/button[2]"), which is captured AFTER
        // act() already performed the click -- if that click's own effect re-rendered
        // the surrounding DOM (e.g. a quantity-stepper click adding/removing a sibling
        // element), the exact same position no longer holds the element by the time we
        // re-resolve it here, even though the click itself genuinely succeeded live.
        // Confirmed via raw debug logs: Stagehand correctly identified a "+" quantity
        // button semantically (its own `description` said so), but the positional
        // XPath came back empty on resolution. Fall back to a semantic search: find
        // element(s) whose own exact text matches a short symbol/word extracted from
        // the action's description (e.g. "+"), and disambiguate among several matches
        // using other words from that description (e.g. "Full price") found in each
        // candidate's own container -- much more resilient to a layout shift than a
        // frozen absolute position.
        if (!hasUsableIdentity(el) && fallbackText) {
          // Real gap found 2026-09-24 (midaticket.com), third round: the real DOM used
          // non-breaking spaces ( , from an `&nbsp;` in the markup) inside "9:30 AM -
          // 7:00 PM", so a strict `=== fallbackText` comparison against Stagehand's
          // normally-spaced description text never matched at all. Normalize whitespace on
          // both sides before comparing.
          const normalizeWs = (s) => (s || "").replace(/ /g, " ").trim().replace(/\s+/g, " ");
          const normalizedTarget = normalizeWs(fallbackText);
          const textCandidates = Array.from(document.querySelectorAll("button, a, span, div, label")).filter(
            (e) => e.children.length === 0 && normalizeWs(e.textContent) === normalizedTarget
          );
          // Real gap found 2026-09-24 (midaticket.com): a "+"/"-" control is very often an
          // ICON FONT glyph (e.g. Font Awesome's `<span class="fa fa-plus">`), rendered via
          // CSS content on a pseudo-element -- its OWN textContent is empty, so a literal
          // text match never finds it no matter how the DOM shifts. Also look for a button
          // whose direct child carries a common plus/minus icon class, or that itself has a
          // `data-dir="up"/"down"`-style attribute (confirmed real markup: a quantity
          // stepper here uses exactly `data-dir="up"`/`data-dir="dwn"`, which is actually a
          // MORE stable identifier than either text or position since it directly encodes
          // increase-vs-decrease intent).
          const isPlus = /^[+]$/.test(fallbackText);
          const isMinus = /^[\-−–]$/.test(fallbackText);
          const iconSelector = isPlus
            ? '[class*="plus"], [data-dir="up"], [aria-label*="increase" i], [aria-label*="plus" i]'
            : isMinus
              ? '[class*="minus"], [data-dir="dwn"], [data-dir="down"], [aria-label*="decrease" i], [aria-label*="minus" i]'
              : null;
          // The icon itself is very often a non-clickable <span> INSIDE the real button (e.g.
          // Font Awesome's <span class="fa fa-plus"> sitting inside <button data-dir="up">) --
          // resolve UP to the nearest clickable ancestor rather than assuming the matched
          // element is itself the target.
          const iconCandidates = iconSelector
            ? Array.from(document.querySelectorAll(iconSelector))
                .map((e) => e.closest("button, a") || e)
                .filter((e, i, arr) => arr.indexOf(e) === i)
            : [];
          const candidates = [...textCandidates, ...iconCandidates];
          if (candidates.length === 1) {
            el = candidates[0];
          } else if (candidates.length > 1 && disambiguators.length > 0) {
            el = candidates.find((c) => {
              const containerText = (c.closest("tr, li, .row, [class*='row'], [class*='option']")?.textContent || c.parentElement?.textContent || "").toLowerCase();
              return disambiguators.some((d) => containerText.includes(d.toLowerCase()));
            }) || null;
          } else if (candidates.length > 1) {
            el = candidates[0];
          }
          // Real gap found 2026-09-24 (midaticket.com), third round: the matched element
          // itself can ALSO have no id/name/class (a bare `<span>` of display text) -- when
          // that text sits inside a `<label>` wrapping a real form control (a very common
          // "clickable label toggles an input" pattern, e.g. a time-slot radio button), the
          // input itself is a far more stable, semantically real target than the text span,
          // and it click-toggles the same way a real user clicking the label would.
          if (el && !hasUsableIdentity(el)) {
            const input = el.closest("label")?.querySelector("input, select, textarea");
            if (input && hasUsableIdentity(input)) el = input;
          }
        }
        if (!el) return null;
        // Real gap found 2026-09-23: Stage 2 was guessing at generic widget markup
        // (e.g. assuming jQuery-UI datepicker class names) with no real DOM to check
        // against, since dom_snapshot was always null. Capture the element's PARENT
        // outerHTML (not just the element itself) -- for a calendar/list widget this
        // includes sibling cells, which is exactly what's needed to write an accurate
        // XPath/CSS selector against the real structure instead of a guessed one.
        //
        // Real gap found 2026-09-24 (midaticket.com): a plain `.closest("table, ul,
        // [role='grid'], [role='listbox']")` misses a very common non-semantic repeated-row
        // widget shape (a plain `<div class="tariff-option">` per row, no table/list
        // semantics at all) -- falling back to just `el.parentElement` in that case
        // (a `.number-spinner` wrapper) loses the row's own distinguishing label (e.g. a
        // sibling `<span class="title">Full price</span>`) that a scoped selector needs.
        // Widen the boundary to also match a handful of common "repeated row/item" class
        // name conventions.
        const container =
          el.closest("table, ul, [role='grid'], [role='listbox'], [class*='option'], [class*='row'], [class*='item']") ||
          el.parentElement ||
          el;
        return {
          id: el.id || null,
          name: el.getAttribute?.("name") || null,
          placeholder: el.getAttribute?.("placeholder") || null,
          className: el.className || null,
          // Real gap found 2026-09-24 (midaticket.com): the real target element can have
          // NO usable id/name/class at all (confirmed: `<button class="" data-dir="up">`)
          // while still carrying a stable, semantically meaningful data-* attribute --
          // capture the most common intent-encoding ones so findEl has something to work
          // with beyond a fragile absolute position.
          dataDir: el.getAttribute?.("data-dir") || null,
          ariaLabel: el.getAttribute?.("aria-label") || null,
          containerHtml: container ? container.outerHTML : null,
          text: (el.textContent || "").trim().slice(0, 20),
        };
      }, buildResolutionArgs(rawSelector, action.description));
      if (resolved?.id) candidates.push(`#${resolved.id}`);
      if (resolved?.name) candidates.push(`[name="${resolved.name}"]`);
      if (resolved?.placeholder) candidates.push(`[placeholder="${resolved.placeholder}"]`);
      if (resolved?.className) {
        const cls = resolved.className.split(/\s+/).filter(Boolean).join(".");
        if (cls) candidates.push(`.${cls}`);
      }
      // Real gap found 2026-09-24 (midaticket.com): the target can have no id/name/class at
      // all while still carrying a stable data-* attribute (e.g. `data-dir="up"` on a
      // quantity-stepper button) -- note this candidate CAN be ambiguous alone (the same
      // data-dir value is likely reused across other repeated rows on the page), so it's
      // added last, after more specific candidates, and containerHtml (below) carries the
      // real surrounding markup so Stage 2 can additionally scope it to the right row.
      if (resolved?.dataDir) candidates.push(`[data-dir="${resolved.dataDir}"]`);
      if (resolved?.ariaLabel) candidates.push(`[aria-label="${resolved.ariaLabel}"]`);
      if (resolved?.containerHtml) {
        domSnapshot = resolved.containerHtml.length > DOM_SNAPSHOT_MAX_CHARS
          ? resolved.containerHtml.slice(0, DOM_SNAPSHOT_MAX_CHARS) + "\n<!-- truncated -->"
          : resolved.containerHtml;
      }
      resolvedText = resolved?.text ?? null;
    } catch (err) {
      debug(`step ${index} page.evaluate resolution failed:`, err?.message ?? err);
    }
  }
  if (candidates.length === 0) {
    debug(`step ${index} FAILED to resolve any candidates for rawSelector=`, JSON.stringify(rawSelector));
  }

  return {
    index,
    action: isFill ? "fill" : "click",
    url: portalUrl,
    selector_candidates: candidates,
    dom_snapshot: domSnapshot,
    screenshot: null,
    field_to_booking_key: fieldKey ?? null,
    requires_credentials: Boolean(extra.requiresCredentials),
    credential_key: extra.requiresCredentials ? (index === 0 ? "username" : "password") : null,
    // Not part of the funnel schema -- stripped by the caller before push(). Only used to
    // let the customInstructions retry loop tell a cumulative stepper click ("+"/"-") apart
    // from a toggle/selection click that repeating would misinterpret as no progress.
    _elementText: resolvedText,
  };
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.on("data", (c) => (data += c));
    process.stdin.on("end", () => resolve(data));
  });
}

main();
