/**
 * Zendesk agent ticket content script (works on any `*.zendesk.com` org).
 *
 * Renders a fixed floating button — placement is anchored to the viewport, not
 * to any Zendesk DOM/text, so it's robust across orgs and layout changes. On
 * click it reads the Booking id from the active ticket and asks the service
 * worker to run the booking-autofill pipeline.
 */
(() => {
  "use strict";

  // Mirrors of lib/messages.js — content scripts can't import modules. Keep in sync.
  const RUN_BOOKING_AUTOFILL = "RUN_BOOKING_AUTOFILL";
  const PAYMENT_DECISION = "PAYMENT_DECISION";
  const STATUS = { PAYMENT_REQUIRED: "PAYMENT_REQUIRED", CONFIRMED: "CONFIRMED", ERROR: "ERROR" };

  const CONTROL_ID = "hd-autofill-control";
  const BUTTON_ID = "hd-autofill-btn";
  const IDLE_LABEL = "Automate Booking";
  const BUSY_LABEL = "Automating…";
  const FLASH_MS = 3000;

  /** Button mode: "run" starts the booking; "confirm" accepts the agent payment answer. */
  let mode = "run";

  /**
   * Visible text of the active ticket, one text node per line, plus any visible link
   * hrefs. Reads only visible nodes so hidden ticket panes (other open tabs) don't
   * interfere.
   */
  function getVisibleTicketText() {
    // Zendesk's workspace tab strip lists EVERY open ticket's title, including its
    // "(Booking ID <n>)" -- visible, and earlier in the DOM than the active ticket, so with
    // two ticket tabs open the other ticket's booking id (and the wrong booking) won.
    const IN_TAB_STRIP = '[data-test-id^="header-tab"]';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const el = node.parentElement;
        return el && el.offsetParent !== null && !el.closest(IN_TAB_STRIP)
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_SKIP;
      },
    });

    const parts = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      parts.push(node.nodeValue);
    }
    document.querySelectorAll("a[href]").forEach((a) => {
      if (a.offsetParent !== null) parts.push(a.getAttribute("href"));
    });
    return parts.join("\n");
  }

  /**
   * Booking id from the active ticket. Prefers an explicit "Booking Id: <n>" in
   * the visible text, then the document title ("Booking id: <n>"), then any
   * visible "Booking: <n>", then a bare BMS/Aries link
   * (`.../bms/<n>` or `.../bms/booking/<n>`). The link fallback exists for tickets
   * whose comments carry nothing but "BMS link: https://box-office.headout.com/bms/<n>"
   * -- no "Booking" wording anywhere for the other patterns to match.
   */
  function getBookingId() {
    const body = getVisibleTicketText();

    return (
      body.match(/Booking\s*[_ ]?Id\s*[:#]?\s*(\d{4,})/i)?.[1] ??
      document.title.match(/Booking\s*[_ ]?id\s*[:#]?\s*(\d{4,})/i)?.[1] ??
      body.match(/Booking:\s*(\d{4,})/i)?.[1] ??
      body.match(/headout\.com\/bms\/(?:booking\/)?(\d{4,})/i)?.[1] ??
      null
    );
  }

  /**
   * Pseudo (volitand / veritt / gmail) email an ops agent leaves in an internal
   * ticket comment as "Pseudo Mail ID: <email>". Vendors must be given this instead
   * of the guest's real email. Keyed on the label rather than the domain: tickets
   * also carry unrelated veritt addresses (e.g. the vendor's "Booking Intimation
   * Email"), which must not be picked up. Falls back to an address containing this
   * booking's id (`name_<bookingId>@...`) if the label is missing.
   */
  function getPseudoEmail(bookingId) {
    const body = getVisibleTicketText();
    const labelled = body.match(/Pseudo\s*Mail(?:\s*ID)?\s*[:\-]\s*([^\s|,;<>]+@[^\s|,;<>]+)/i)?.[1];
    const byId = bookingId
      ? body.match(new RegExp(`[\\w.+-]+_${bookingId}@[\\w.-]+\\.[A-Za-z]{2,}`, "i"))?.[0]
      : null;
    const email = labelled ?? byId ?? null;
    return email ? email.replace(/[.)\]]+$/, "") : null;
  }

  function sendMessage(message) {
    return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));
  }

  function setLabel(button, text, isError = false) {
    button.textContent = text;
    button.classList.toggle("hd-autofill-btn--error", isError);
  }

  function flashError(button, text) {
    setLabel(button, text, true);
    setTimeout(() => setLabel(button, IDLE_LABEL), FLASH_MS);
  }

  function describeError(code) {
    switch (code) {
      case "NOT_AUTHENTICATED":
        return "Not logged in to Headout";
      case "USER_SCRIPTS_DISABLED":
        return "Enable 'Allow user scripts'";
      case "AUTOMATION_API_MISSING":
        return "Script has no automation API";
      default:
        return `Failed${code ? `: ${code}` : ""}`;
    }
  }

  /** Switch the button into the post-payment "confirm" mode. */
  function enterConfirmMode(control) {
    mode = "confirm";
    control.classList.add("hd-autofill-control--confirm");
    setConfirmStatus(control, "Script worked?");
  }

  function resetToRun(button, label = IDLE_LABEL) {
    mode = "run";
    button.closest(`#${CONTROL_ID}`)?.classList.remove("hd-autofill-control--confirm");
    setLabel(button, label);
  }

  /**
   * Real gap found 2026-10-07: getBookingId() used to be checked exactly once, synchronously,
   * at click time. The floating button renders immediately on page load (its own
   * MutationObserver doesn't wait for ticket content), but Zendesk's own SPA can still be
   * asynchronously loading the ticket's subject/body/sidebar fields at that exact moment --
   * especially right after opening a freshly-created ticket. Confirmed live: a click in that
   * window failed with "Booking ID not found" on a ticket whose booking ID was present and
   * extractable moments later, unchanged. Poll briefly instead of checking once.
   */
  async function pollFor(read, deadlineMs = 3000, intervalMs = 300) {
    const deadline = Date.now() + deadlineMs;
    let value = read();
    while (!value && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, intervalMs));
      value = read();
    }
    return value;
  }

  const getBookingIdWithRetry = () => pollFor(getBookingId);

  /** Start the booking: booking id -> vendor link -> automation up to payment. */
  async function runBooking(button) {
    // Disabled immediately, before the booking-id retry even starts -- otherwise the button
    // sits clickable (looking idle) for up to 3s while getBookingIdWithRetry polls, and a
    // second click in that window would kick off a second, overlapping run.
    button.disabled = true;
    setLabel(button, BUSY_LABEL);

    const bookingId = await getBookingIdWithRetry();
    if (!bookingId) {
      button.disabled = false;
      flashError(button, "Booking ID not found");
      return;
    }

    const pseudoEmail = await pollFor(() => getPseudoEmail(bookingId));
    if (!pseudoEmail) {
      button.disabled = false;
      flashError(button, "Pseudo mail not found in ticket comments");
      return;
    }

    const response = await sendMessage({ type: RUN_BOOKING_AUTOFILL, bookingId, pseudoEmail });
    button.disabled = false;

    if (!response?.ok) {
      flashError(button, describeError(response?.error));
      return;
    }
    const result = response.result ?? {};
    // Real bug found 2026-10-07: this used to be a plain if/else on CONFIRMED, which put
    // EVERY non-CONFIRMED status -- PAYMENT_REQUIRED (genuine success, awaiting manual
    // payment) AND ERROR (the script actually failed) alike -- into the same "enterConfirmMode"
    // branch. Confirmed live: a run that failed on an early step (vendor tab never left the
    // homepage) still showed the "Script worked?" Yes/No prompt, exactly as if it had reached
    // payment. ERROR must flash the real failure instead of asking the agent to confirm
    // something that never happened.
    if (response.status === STATUS.CONFIRMED) {
      flashSuccess(button, result.reference ? `Booked ✓ ${result.reference}` : "Booked ✓");
    } else if (response.status === STATUS.ERROR) {
      flashError(button, result.error ? `Failed: ${result.error}` : "Automation failed");
    } else {
      enterConfirmMode(button.closest(`#${CONTROL_ID}`));
    }
  }

  /** After the agent has paid manually, accept the agent's answer directly. */
  async function confirmPayment(button, paid) {
    const control = button.closest(`#${CONTROL_ID}`);
    button.disabled = true;
    setConfirmButtonsDisabled(control, true);
    setConfirmStatus(control, paid ? "Accepting yes..." : "Accepting no...");

    const response = await sendMessage({ type: PAYMENT_DECISION, paid });

    button.disabled = false;
    setConfirmButtonsDisabled(control, false);

    const result = response?.result ?? {};
    if (response?.ok && response.status === STATUS.CONFIRMED) {
      resetToRun(button);
      flashSuccess(button, "Booked ✓");
      return;
    }

    if (response?.ok && response.status === STATUS.PAYMENT_REQUIRED) {
      setConfirmStatus(control, "Payment not confirmed");
      return;
    }

    const code = response?.ok ? result.error || result.code : response?.error;
    setConfirmStatus(control, describeError(code), true);
  }

  function flashSuccess(button, text) {
    setLabel(button, text);
    setTimeout(() => resetToRun(button), FLASH_MS);
  }

  function onClick(event) {
    const button = event.currentTarget;
    return mode === "confirm" ? undefined : runBooking(button);
  }

  function setConfirmStatus(control, text, isError = false) {
    const status = control?.querySelector(".hd-payment-confirm__status");
    if (!status) return;

    status.textContent = text;
    status.classList.toggle("hd-payment-confirm__status--error", isError);
  }

  function setConfirmButtonsDisabled(control, disabled) {
    control
      ?.querySelectorAll(".hd-payment-confirm__choice")
      .forEach((button) => {
        button.disabled = disabled;
      });
  }

  function createControl() {
    const control = document.createElement("div");
    control.id = CONTROL_ID;
    control.className = "hd-autofill-control";

    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.type = "button";
    button.className = "hd-autofill-btn";
    button.textContent = IDLE_LABEL;
    button.addEventListener("click", onClick);

    const confirm = document.createElement("div");
    confirm.className = "hd-payment-confirm";

    const status = document.createElement("span");
    status.className = "hd-payment-confirm__status";
    status.textContent = "Payment completed?";

    const yesButton = document.createElement("button");
    yesButton.type = "button";
    yesButton.className = "hd-payment-confirm__choice hd-payment-confirm__choice--yes";
    yesButton.textContent = "Yes";
    yesButton.addEventListener("click", () => confirmPayment(button, true));

    const noButton = document.createElement("button");
    noButton.type = "button";
    noButton.className = "hd-payment-confirm__choice hd-payment-confirm__choice--no";
    noButton.textContent = "No";
    noButton.addEventListener("click", () => confirmPayment(button, false));

    confirm.append(status, yesButton, noButton);
    control.append(button, confirm);
    return control;
  }

  /** Keep the floating button present (re-add if the SPA ever removes it). */
  function ensureButton() {
    if (!document.getElementById(CONTROL_ID)) {
      document.body.appendChild(createControl());
    }
  }

  new MutationObserver(ensureButton).observe(document.body, { childList: true });
  ensureButton();
})();
