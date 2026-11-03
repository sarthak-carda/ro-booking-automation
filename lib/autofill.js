/**
 * Fetch the form-autofill script from the local service and run it on a tab.
 *
 * We use the `chrome.userScripts` API rather than `chrome.scripting` + eval:
 * MV3's CSP blocks `eval`/`new Function` in the content-script world, and a
 * strict page CSP (e.g. Google Forms) blocks it in MAIN. `userScripts` is the
 * sanctioned way to execute remotely-fetched code — it requires the user to
 * enable "Allow user scripts" on the extension's details page.
 *
 * POC BUILD (Prado + TOSC/midaticket.com only): `fetchAutofillScript` below does NOT
 * call automate-server — it returns one of two scripts hardcoded in
 * ./known-scripts.js (pulled verbatim from automate-server's real, already-shipped
 * script_registry.json), keyed by the vendor link's host. This is the ONLY place in
 * the extension that ever talked to automate-server for a script, so this one function
 * is what makes automate-server unnecessary for this build. Aries (booking/guest-detail
 * lookup) and the Zendesk/Ory login are UNCHANGED and still hit the real network — only
 * script-serving is removed. Do not confuse this build with the real extension; see
 * manifest.json's name/version and the README for how this variant is marked.
 */
import { ERROR_CODES } from "./messages.js";
import { KNOWN_SCRIPTS } from "./known-scripts.js";
import { waitForTabLoad } from "./tabs.js";
import { hostOf } from "./vendors.js";

/**
 * A generated script ends each funnel step by triggering a navigation and then never
 * resolving, so the page teardown re-injects the next step. This happens in two shapes:
 *   1. A cross-host language redirect in `ensureLanguage()` (e.g. Széchenyi:
 *      jegyek.szechenyifurdo.hu -> tickets.szechenyibath.hu).
 *   2. A multi-step, full-postback funnel (e.g. JTB Web Connect, classic ASP.NET
 *      WebForms): OptionInfo "Update" -> OptionInfo "Book now" -> AddService
 *      "Book service" -> Shopping-Cart. The "Update" recompute is a `__doPostBack`
 *      that reloads the SAME URL, so we must NOT gate re-injection on the URL string
 *      changing.
 * Either way the navigation tears down the USER_SCRIPT context and `userScripts.execute`
 * rejects before the script finishes. We wait for the new document, re-inject, and invoke
 * again. The cap must cover the longest funnel (4 contexts above) plus a redirect, with
 * headroom; it still stops a genuinely-broken script from looping forever.
 */
const MAX_INJECTION_ATTEMPTS = 6;

/**
 * Look up the autofill script for the vendor link's host from the hardcoded
 * KNOWN_SCRIPTS table (see the POC note above) — no network call.
 * @param {string} link
 * @returns {Promise<string>} The script source.
 */
export async function fetchAutofillScript(link) {
  const host = hostOf(link);
  const script = KNOWN_SCRIPTS[host];
  if (!script) {
    throw new Error(`No script for this vendor (POC build only supports: ${Object.keys(KNOWN_SCRIPTS).join(", ")})`);
  }
  return script;
}

/**
 * Run the script on the tab and drive the booking up to payment.
 *
 * The generated script (see the generate-automate-script skill) registers
 * `window.HeadoutAutomation` and exposes `run(booking)`, which walks the vendor
 * funnel and resolves to `{ status, ... }` — `PAYMENT_REQUIRED` when it reaches
 * the payment page (it never pays). We invoke it in the same USER_SCRIPT world,
 * which shares the page DOM, and return the script's structured result.
 *
 * A legacy fallback to `window.HeadoutOrderFormAutofill.fill(booking)` is kept so
 * the mock-server's older autofill scripts still work in development.
 *
 * @param {number} tabId
 * @param {string} script
 * @param {unknown} booking
 * @param {{ credentials?: { username: string, password: string } | null }} [ctx] Passed as
 *   `run(booking, ctx)`. For vendors behind a login, `ctx.credentials` is the login the agent
 *   saved this browser session (lib/credentials.js) or null. It is serialised into the injected
 *   code string, which only ever runs in the isolated USER_SCRIPT world -- never log `ctx`.
 * @returns {Promise<object>} The script's result (`{ status, ... }`).
 */
export async function runAutofillOnTab(tabId, script, booking, ctx = {}) {
  const result = await executeWithNavigationRetry(tabId, script, runCall(booking, ctx));
  // Some checkouts (Bran Castle's accesso Pay) put the contact/billing form in a cross-origin
  // iframe the page-level script cannot reach. Such a script returns `frameFill: true` and
  // exposes `HeadoutAutomation.fillFrame(booking)`, which we run inside every frame of the tab.
  if (result?.status === "PAYMENT_REQUIRED" && result.frameFill) {
    const { frameFill, ...rest } = result;
    return { ...rest, contactFrame: await runFramePhase(tabId, script, booking) };
  }
  return result;
}

/**
 * Run `fillFrame(booking)` in all frames; the script returns null everywhere except in the
 * frame it knows. Never throws: a failure here must not hide that the cart is ready.
 * @returns {Promise<{ filled?: string[], missed?: string[], error?: string }>}
 */
async function runFramePhase(tabId, script, booking) {
  const userScripts = getUserScriptsApi();
  if (!userScripts) return { error: ERROR_CODES.USER_SCRIPTS_DISABLED };
  const call = `(() => {
  const a = window.HeadoutAutomation;
  if (!a || typeof a.fillFrame !== "function") return null;
  return Promise.resolve(a.fillFrame(${JSON.stringify(booking)})).catch((e) => ({ error: String((e && e.message) || e) }));
})();`;
  try {
    const results = await userScripts.execute({
      target: { tabId, allFrames: true },
      world: "USER_SCRIPT",
      js: [{ code: `${script}\n;${call}` }],
    });
    // Several frames can answer (nested checkout frames); a field counts as filled when any did it.
    const hits = (results ?? []).map((r) => r?.result).filter((r) => r && typeof r === "object");
    if (!hits.length) return { missed: ["payment frame not found: fill the contact and billing details by hand"] };
    const filled = [...new Set(hits.flatMap((h) => h.filled ?? []))];
    const missed = [...new Set(hits.flatMap((h) => h.missed ?? []))].filter((m) => !filled.includes(m));
    return { filled, missed, frames: hits };
  } catch (e) {
    return { error: `Could not fill the payment frame: ${e?.message ?? e}` };
  }
}

/**
 * Inject + invoke, tolerating an in-script navigation between funnel steps. Such a
 * navigation destroys the USER_SCRIPT context, so `userScripts.execute` rejects mid-run
 * (or, if injection raced the reload, resolves with the "API missing" fallback); either
 * way we wait for the new document and re-inject the next step.
 *
 * Crucially we do NOT gate the retry on the tab's URL string changing. The funnel step
 * that re-injects can be a full-document reload to the SAME URL (an ASP.NET WebForms
 * `__doPostBack`, e.g. JTB Web Connect's "Update" recompute). A bare rejection from
 * `userScripts.execute` already means the context was torn down — a genuine error inside
 * `run()` is caught there and surfaces as a RESOLVED `{ status: "ERROR" }`, never as a
 * rejection — so retrying on any rejection is correct and the URL is irrelevant.
 *
 * SPA route changes within the same document (cart -> billing -> summary) do NOT tear
 * down the context, so such a funnel still runs in a single injection.
 */
async function executeWithNavigationRetry(tabId, script, invocation) {
  let lastError;
  for (let attempt = 0; attempt < MAX_INJECTION_ATTEMPTS; attempt++) {
    const lastAttempt = attempt === MAX_INJECTION_ATTEMPTS - 1;
    try {
      const result = await executeInScriptWorld(tabId, script, invocation);
      // The script API looks absent: injection most likely raced a reload (a postback or
      // a language redirect). While we still have budget, wait for the page and re-inject.
      if (isMissingApiResult(result) && !lastAttempt) {
        await waitForTabLoad(tabId);
        continue;
      }
      return result;
    } catch (err) {
      // Don't retry a configuration failure (user scripts not enabled).
      if (err?.message === ERROR_CODES.USER_SCRIPTS_DISABLED) throw err;
      lastError = err;
      // The context was torn down by an in-script navigation/postback. Re-inject the next
      // step on the new document (no URL-change check — postbacks reuse the same URL).
      if (lastAttempt) break;
      await waitForTabLoad(tabId);
    }
  }
  return {
    status: "ERROR",
    error: `Script kept navigating without completing${lastError ? `: ${lastError.message ?? lastError}` : ""}`,
  };
}

/** Inject `script` + a trailing `invocation` expression and return its resolved value. */
async function executeInScriptWorld(tabId, script, invocation) {
  const userScripts = getUserScriptsApi();
  if (!userScripts) throw new Error(ERROR_CODES.USER_SCRIPTS_DISABLED);

  const results = await userScripts.execute({
    target: { tabId },
    world: "USER_SCRIPT",
    js: [{ code: `${script}\n;${invocation}` }],
  });

  // `userScripts.execute` awaits a returned promise and surfaces its value.
  const result = results?.[0]?.result;
  return result ?? { status: "ERROR", error: ERROR_CODES.AUTOMATION_API_MISSING };
}

/** True for the "no HeadoutAutomation API found" fallback result. */
function isMissingApiResult(result) {
  return (
    result &&
    typeof result === "object" &&
    result.status === "ERROR" &&
    result.error === ERROR_CODES.AUTOMATION_API_MISSING
  );
}

/** `chrome.userScripts` throws on access when the API is disabled; treat as null. */
function getUserScriptsApi() {
  try {
    return chrome.userScripts ?? null;
  } catch {
    return null;
  }
}

/**
 * Expression that invokes the booking run, preferring the HeadoutAutomation
 * contract and falling back to the legacy autofill API. The IIFE returns the
 * result so `userScripts.execute` can surface it back to the service worker.
 */
function runCall(booking, ctx) {
  const json = JSON.stringify(booking);
  const ctxJson = JSON.stringify(ctx ?? {});
  return `(() => {
  const automation = window.HeadoutAutomation;
  if (automation && typeof automation.run === "function") {
    return Promise.resolve(automation.run(${json}, ${ctxJson})).catch((e) => ({ status: "ERROR", error: String(e && e.message || e) }));
  }
  const legacy = window.HeadoutOrderFormAutofill;
  if (legacy && typeof legacy.fill === "function") {
    return Promise.resolve(legacy.fill(${json}))
      .then((result) => ({ status: "CONFIRMED", legacy: true, result }))
      .catch((e) => ({ status: "ERROR", error: String(e && e.message || e) }));
  }
  return { status: "ERROR", error: "${ERROR_CODES.AUTOMATION_API_MISSING}" };
})();`;
}
