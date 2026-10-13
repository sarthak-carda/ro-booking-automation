"""Stage 3 — Audit gate.

Ports src/pipeline/audit.js exactly. Headless Playwright replay of the
generated script against a test booking. Mirrors the extension's own
re-injection behavior (lib/autofill.js `executeWithNavigationRetry`) so a
multi-step, multi-navigation funnel is audited the same way it will actually
run: a navigation between funnel steps tears down the page context, so we
wait for the new document and re-inject, instead of treating that as a
script failure.

Fails loudly: any status other than PAYMENT_REQUIRED, or exhausting every
retry attempt, is a hard failure with a report explaining why — never a
silent pass. This is the safety-critical gate that guarantees a generated
script never completes a real payment on a live vendor site (the script
itself must stop at PAYMENT_REQUIRED; this module only proves that it does).

SAFETY-CRITICAL: do not weaken the "anything other than PAYMENT_REQUIRED (or
exhausting attempts) is pass=False" contract below.
"""
import asyncio
import contextlib
import os
from typing import Any, Optional

from playwright.async_api import async_playwright

MAX_INJECTION_ATTEMPTS = 6

# Runs inside the page (Playwright serializes this expression + arg across the
# boundary, same as the JS original's `runInPage` function passed to
# page.evaluate). Written as a JS arrow-function-expression string because
# Python cannot hand Playwright a Python function to run in-browser.
_RUN_IN_PAGE_JS = """
({ script, booking, credentials }) => {
  new Function(script)();
  const automation = window.HeadoutAutomation;
  if (!automation || typeof automation.run !== "function") {
    return { status: "MISSING_API" };
  }
  return Promise.resolve(automation.run(booking, { credentials })).catch((e) => ({
    status: "ERROR",
    error: String((e && e.message) || e),
  }));
}
"""


async def _await_navigation(nav_cm) -> bool:
    """Consumes a `page.expect_navigation(...)` context manager that was already
    armed (created) before the caller's action, mirroring JS's
    `page.waitForNavigation({...}).catch(() => null)` — resolves to True if a
    navigation actually completed before its own timeout, False otherwise
    (timeout, or any other navigation error), never raises.
    """
    try:
        async with nav_cm:
            pass
        return True
    except Exception:
        return False


async def _safe_content(page) -> Optional[str]:
    """`page.content()` can throw if the page is mid-navigation or already
    closed — a failure here must never mask the real audit failure, so this
    swallows any error and returns None instead. Feeds Stage 2.5's
    (app/pipeline/repair.py) `detect_environment_blocked`, which needs the DOM
    alongside the error string: a CAPTCHA/anti-bot page produces the exact same
    symptom as real selector drift (the expected element never renders), so
    the error text alone can't tell the two apart."""
    try:
        return await page.content()
    except Exception:
        return None


async def _cancel_task(task: "asyncio.Task") -> None:
    """Python (unlike JS) can warn/hang on a Task that's abandoned mid-flight
    while the browser it's watching gets closed, so — unlike the JS original,
    which just lets an unneeded navigationPromise dangle and get garbage
    collected — we explicitly cancel it once we know we no longer care about
    its result. This changes nothing about the audit's pass/fail outcome,
    only Python-side task cleanup.
    """
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def audit_script(
    *,
    script: str,
    booking: dict,
    portal_url: str,
    credentials: Optional[dict] = None,
    screenshot_dir: Optional[str] = None,
) -> dict:
    """@param script - generated HeadoutAutomation script source
    @param booking - test booking payload
    @param portal_url - vendor URL, or a `file://` / localhost URL for an offline fixture
    @param credentials - test/sandbox credentials, never production
    @param screenshot_dir - where to write step screenshots
    @returns {"pass": bool, "status": str, "error": str | None, "screenshots": list[str],
      "dom": str | None, "debug": dict | None} -- "dom"/"debug" are only populated on a
      failing result (a DOM/debug snapshot of a PASSING run isn't needed by anything;
      see app/pipeline/repair.py's detect_environment_blocked, the consumer "dom" exists
      for -- a CAPTCHA/anti-bot page produces the identical symptom as real selector
      drift, so the error string alone can't distinguish them).
    """
    credentials = credentials or {}
    screenshots: list[str] = []

    async with async_playwright() as p:
        # --no-sandbox: some CI/dev sandboxes block Chromium's own sandbox syscalls;
        # this process already runs inside its own restricted environment.
        #
        # Real finding (2026-09-23, Prado): headless Chromium gets outright blocked by
        # Cloudflare on some real vendor sites (served a "cf-cookie-error" challenge page
        # instead of the real one) -- confirmed by testing headless vs headed with an
        # otherwise-identical launch: headless got the Cloudflare block every time (even
        # after 12s), headed loaded the real page immediately. This is standard, well-known
        # anti-bot behavior keying off the headless flag itself, not a captcha or anything
        # requiring a human to solve -- running headed here is a normal configuration choice,
        # not evasion. Default stays headless (fast, no display needed -- fine for the
        # offline test suite's own fixtures, which don't have this problem); set
        # AUDIT_HEADLESS=false for a real vendor run on a machine with a real display.
        headless = os.environ.get("AUDIT_HEADLESS", "true").lower() != "false"
        browser = await p.chromium.launch(args=["--no-sandbox"], headless=headless)
        try:
            page = await browser.new_page()
            await page.goto(portal_url, wait_until="domcontentloaded")

            last_result: Optional[dict] = None
            for attempt in range(MAX_INJECTION_ATTEMPTS):
                # Arm the navigation waiter BEFORE evaluating, exactly like the JS
                # original: `page.expect_navigation(...)` registers its listener
                # synchronously the moment it's called (see Playwright's
                # Frame.expect_navigation implementation), not when awaited — so
                # creating it here and consuming it later races it against
                # `page.evaluate` below instead of serializing after it, the same
                # way JS's `page.waitForNavigation()` promise does.
                nav_cm = page.expect_navigation(wait_until="domcontentloaded", timeout=4000)
                navigation_task = asyncio.create_task(_await_navigation(nav_cm))

                try:
                    result: Any = await page.evaluate(
                        _RUN_IN_PAGE_JS,
                        {"script": script, "booking": booking, "credentials": credentials},
                    )
                except Exception:
                    # A navigation mid-evaluate throws ("Execution context was
                    # destroyed"). Treat exactly like the extension does: wait for
                    # the new document, retry — NOT gated on the URL string
                    # changing (a __doPostBack-style reload can hit the same URL).
                    await navigation_task
                    continue

                if screenshot_dir:
                    shot_path = os.path.join(screenshot_dir, f"attempt-{attempt}.png")
                    try:
                        await page.screenshot(path=shot_path)
                        screenshots.append(shot_path)
                    except Exception:
                        pass

                if isinstance(result, dict) and result.get("status") == "MISSING_API":
                    navigated = await navigation_task
                    if navigated:
                        continue
                else:
                    # We're about to break out with this result — the navigation
                    # waiter is no longer needed, so stop it instead of leaving it
                    # to time out on its own against a page we're about to close.
                    await _cancel_task(navigation_task)

                last_result = result
                break

            if not last_result:
                return {
                    "pass": False,
                    "status": "ERROR",
                    "error": "Script kept navigating without completing",
                    "screenshots": screenshots,
                    "dom": await _safe_content(page),
                }
            if last_result.get("status") == "MISSING_API":
                return {
                    "pass": False,
                    "status": "ERROR",
                    "error": "window.HeadoutAutomation.run was never registered",
                    "screenshots": screenshots,
                    "dom": await _safe_content(page),
                    "debug": last_result.get("debug"),
                }
            if last_result.get("status") != "PAYMENT_REQUIRED":
                return {
                    "pass": False,
                    "status": last_result.get("status"),
                    "error": last_result.get("error") or f"Expected PAYMENT_REQUIRED, got {last_result.get('status')}",
                    "screenshots": screenshots,
                    "dom": await _safe_content(page),
                    "debug": last_result.get("debug"),
                }
            return {"pass": True, "status": "PAYMENT_REQUIRED", "screenshots": screenshots}
        finally:
            await browser.close()
