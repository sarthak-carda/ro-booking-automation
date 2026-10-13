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

  /** A visible BMS link's booking id, e.g. https://box-office.headout.com/bms/34259488 -> "34259488". */
  function getBookingIdFromBmsLink() {
    const bmsPattern = /box-office\.headout\.com\/bms\/(\d+)/i;

    const anchorMatch = Array.from(document.querySelectorAll("a[href]"))
      .filter((a) => a.offsetParent !== null)
      .map((a) => a.href.match(bmsPattern)?.[1])
      .find(Boolean);
    if (anchorMatch) return anchorMatch;

    // The link may appear as plain pasted text (not a real anchor) in a ticket note.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!/box-office\.headout\.com/i.test(node.nodeValue ?? "")) return NodeFilter.FILTER_SKIP;
        const el = node.parentElement;
        return el && el.offsetParent !== null ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const match = node.nodeValue.match(bmsPattern);
      if (match) return match[1];
    }
    return null;
  }

  /**
   * Booking id from the active ticket. Prefers the id from a visible BMS link
   * (https://box-office.headout.com/bms/<id>), the most reliable/structured signal, falling
   * back to an explicit "Booking Id: <n>" in the visible text, then the document title
   * ("Booking id: <n>"), then any visible "Booking: <n>". Reads only visible nodes so hidden
   * ticket panes (other open tabs) don't interfere.
   */
  function getBookingId() {
    const fromBmsLink = getBookingIdFromBmsLink();
    if (fromBmsLink) return fromBmsLink;

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!/booking/i.test(node.nodeValue ?? "")) return NodeFilter.FILTER_SKIP;
        const el = node.parentElement;
        return el && el.offsetParent !== null
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_SKIP;
      },
    });

    const parts = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      parts.push(node.nodeValue);
    }
    const body = parts.join("\n");

    return (
      body.match(/Booking\s*[_ ]?Id\s*[:#]?\s*(\d{4,})/i)?.[1] ??
      document.title.match(/Booking\s*[_ ]?id\s*[:#]?\s*(\d{4,})/i)?.[1] ??
      body.match(/Booking:\s*(\d{4,})/i)?.[1] ??
      null
    );
  }

  // Preference order for the "aiTA" pseudo-mail tool's generated inbox domains: Volitand,
  // then Veritt, then Gmail. Per Headout's RO Lead workflow, a human or macro runs that tool
  // BEFORE our button is clicked and posts "Pseudo Mail ID: <email>" as an internal ticket
  // note -- we read that note rather than trying to drive aiTA's own panel ourselves.
  const PSEUDO_DOMAIN_PRIORITY = ["volitand.com", "veritt.com", "gmail.com"];

  /** Every "Pseudo Mail ID: <email>" value visible anywhere on the ticket, in document order. */
  function getPseudoEmailCandidates() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!/pseudo\s*mail/i.test(node.nodeValue ?? "")) return NodeFilter.FILTER_SKIP;
        const el = node.parentElement;
        return el && el.offsetParent !== null ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const parts = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) parts.push(node.nodeValue);
    const body = parts.join("\n");
    return [...body.matchAll(/Pseudo\s*Mail\s*ID\s*:\s*([^\s]+@[^\s,;]+)/gi)].map((m) =>
      m[1].replace(/[.,;]+$/, "")
    );
  }

  /**
   * Pick one email from `candidates` per the Volitand > Veritt > Gmail preference. Within
   * Gmail specifically, skip any "+alias" address and take the first plain one instead.
   */
  function pickPseudoEmail(candidates) {
    for (const domain of PSEUDO_DOMAIN_PRIORITY) {
      const forDomain = candidates.filter((email) => email.toLowerCase().endsWith(`@${domain}`));
      if (forDomain.length === 0) continue;
      if (domain === "gmail.com") {
        const plain = forDomain.find((email) => !email.includes("+"));
        if (plain) return plain;
        continue;
      }
      return forDomain[0];
    }
    return null;
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

  /** Start the booking: booking id -> vendor link -> automation up to payment. */
  async function runBooking(button) {
    const bookingId = getBookingId();
    if (!bookingId) {
      flashError(button, "Booking ID not found");
      return;
    }

    const pseudoEmail = pickPseudoEmail(getPseudoEmailCandidates());

    button.disabled = true;
    setLabel(button, BUSY_LABEL);
    const response = await sendMessage({ type: RUN_BOOKING_AUTOFILL, bookingId, pseudoEmail });
    button.disabled = false;

    if (!response?.ok) {
      flashError(button, describeError(response?.error));
      return;
    }
    const result = response.result ?? {};
    if (response.status === STATUS.CONFIRMED) {
      flashSuccess(button, result.reference ? `Booked ✓ ${result.reference}` : "Booked ✓");
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
