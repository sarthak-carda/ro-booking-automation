/**
 * Service worker: message router and the booking-autofill pipeline.
 *
 * Pipeline (triggered by the Zendesk button):
 *   1. Fetch booking and guest details from Aries (authenticated).
 *   2. Resolve the vendor link from the vendor-tour endpoint.
 *   3. Fetch the autofill script for that link.
 *   4. Open the vendor link and run the script with the booking data.
 */
import { getEnvironment } from "./config.js";
import { fetchBooking, fetchGuestDetails, resolveVendorLink } from "./lib/aries.js";
import { computeTotalPax, normalizeGuestDetails } from "./lib/booking-normalize.js";
import { resolveGuestCountry } from "./lib/guest-country.js";
import { fetchAutofillScript, runAutofillOnTab } from "./lib/autofill.js";
import { MESSAGES, BOOKING_STATUS, ERROR_CODES } from "./lib/messages.js";
import { clearCredential, credentialStatus, getCredential, setCredential } from "./lib/credentials.js";
import { redactSecrets } from "./lib/redact.js";
import { hostOf, needsPseudoEmail, vendorForLink } from "./lib/vendors.js";
import { isAuthenticated } from "./lib/session.js";
import { createTab, waitForTabLoad } from "./lib/tabs.js";
import { reportBookingEvent } from "./lib/telemetry.js";

const LOG = "[Headout Autofill]";

/**
 * Booking id -> vendor link -> automation up to payment.
 * @param {string|number} bookingId
 * @param {{ cardCountry?: string | null } | undefined} ticketHints Facts read off the Zendesk ticket
 *   that Aries lacks (the card country), used to work out the guest's country.
 * @param {string} pseudoEmail Pseudo (volitand/veritt/gmail) email from the ticket's internal
 *   comments. Replaces the guest's real email on the vendor form, per vendor booking
 *   instructions -- the guest's real address must never be given to a non-partnered vendor.
 * @returns {Promise<{ link: string, status: string, result: object }>}
 */
async function runBookingAutofill(bookingId, pseudoEmail, ticketHints) {
  const env = await getEnvironment();

  const [bookingDetails, rawGuestDetails] = await Promise.all([
    fetchBooking(env, bookingId),
    fetchGuestDetails(env, bookingId),
  ]);
  // Real bug found 2026-10: this used to be a raw passthrough (`{ ...bookingDetails,
  // guestDetails: rawGuestDetails }`) -- see lib/booking-normalize.js's module docstring for
  // the exact real API shapes that made every vendor script's guestDetails.firstName/
  // lastName/email/phone/address reads resolve to undefined, and totalPax silently default
  // to 1, on every real ticket run. Normalize both into the flat shape scripts actually read.
  const guest = normalizeGuestDetails(rawGuestDetails);
  const country = resolveGuestCountry(rawGuestDetails, { phone: guest.phone, cardCountry: ticketHints?.cardCountry });
  const booking = {
    ...bookingDetails,
    totalPax: computeTotalPax(bookingDetails),
    // Never the guest's real address: vendors get the ticket's pseudo mail (or, for vendors whose
    // instructions fix a contact email, the script supplies it and this stays empty).
    guestDetails: { ...guest, email: pseudoEmail ?? "", country, isMalaysian: country.isMalaysian },
  };
  const link = await resolveVendorLink(env, booking);
  if (needsPseudoEmail(link) && !pseudoEmail) {
    throw new Error(ERROR_CODES.PSEUDO_EMAIL_MISSING);
  }
  const script = await fetchAutofillScript(link);

  // Vendors behind a login get whatever the agent saved this browser session (or null). The
  // credential only ever goes into the injected script's `ctx`; every outbound value below is
  // redacted before it is logged, reported or returned to the page.
  const vendor = vendorForLink(link);
  const credentials = vendor?.login ? await getCredential(hostOf(link)) : null;

  const tab = await createTab(vendor?.startUrl ?? link);
  await waitForTabLoad(tab.id);
  const started = Date.now();
  const rawResult = await runAutofillOnTab(tab.id, script, booking, { credentials });
  const result = redactSecrets(rawResult, credentials);
  const durationMs = Date.now() - started;

  const status = result?.status ?? BOOKING_STATUS.ERROR;

  // Best-effort observability report (never throws).
  reportBookingEvent({
    vendorUrl: link,
    userEmail: booking?.agentEmail ?? booking?.email ?? "agent@headout.com",
    userName: booking?.agentName ?? booking?.name,
    bookingId,
    status,
    result,
    durationMs,
  });

  console.log(`${LOG} booking ${bookingId} -> ${status} on ${link}`, result);
  return {
    link,
    status,
    result,
    vendor: vendor?.login ? { host: hostOf(link), label: vendor.label } : null,
  };
}

/**
 * Accept the agent's direct yes/no answer after manual payment.
 * No vendor-side confirmation script is run here.
 * @param {boolean} paid
 * @returns {Promise<{ status: string, result: object }>}
 */
async function acceptPaymentDecision(paid) {
  const status = paid ? BOOKING_STATUS.CONFIRMED : BOOKING_STATUS.PAYMENT_REQUIRED;
  const result = { userConfirmed: Boolean(paid) };

  console.log(`${LOG} payment decision -> ${status}`, result);
  return { status, result };
}

/** Handlers keyed by message type. Each returns the payload sent back to the caller. */
const HANDLERS = {
  [MESSAGES.AUTH_STATUS]: async () => {
    const env = await getEnvironment();
    return { authenticated: await isAuthenticated(env), env: env.key };
  },

  [MESSAGES.OPEN_LOGIN]: async () => {
    const env = await getEnvironment();
    const loginUrl = `${env.oryBase}/ui/login?return_to=${encodeURIComponent(env.eos)}`;
    await createTab(loginUrl);
    return {};
  },

  [MESSAGES.RUN_BOOKING_AUTOFILL]: ({ bookingId, pseudoEmail, ticketHints }) => runBookingAutofill(bookingId, pseudoEmail, ticketHints),

  [MESSAGES.PAYMENT_DECISION]: ({ paid }) => acceptPaymentDecision(Boolean(paid)),

  // Vendor logins (see lib/credentials.js). Replies carry usernames and flags, never passwords.
  [MESSAGES.CREDENTIAL_STATUS]: async () => ({ status: await credentialStatus() }),
  [MESSAGES.SET_CREDENTIAL]: async ({ host, username, password }) => {
    await setCredential(host, { username, password });
    return { status: await credentialStatus() };
  },
  [MESSAGES.CLEAR_CREDENTIAL]: async ({ host }) => {
    await clearCredential(host);
    return { status: await credentialStatus() };
  },
};

/** Credential messages are for extension pages only (popup / credentials panel), never content scripts. */
const EXTENSION_ONLY = new Set([MESSAGES.CREDENTIAL_STATUS, MESSAGES.SET_CREDENTIAL, MESSAGES.CLEAR_CREDENTIAL]);
const isExtensionPage = (sender) => sender?.id === chrome.runtime.id && (sender.url ?? "").startsWith(chrome.runtime.getURL(""));

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handler = HANDLERS[message?.type];
  if (!handler) return false;
  if (EXTENSION_ONLY.has(message.type) && !isExtensionPage(sender)) {
    sendResponse({ ok: false, error: "FORBIDDEN" });
    return false;
  }

  Promise.resolve(handler(message))
    .then((data) => sendResponse({ ok: true, ...data }))
    .catch((error) => {
      console.error(`${LOG} ${message.type} failed:`, error);
      sendResponse({ ok: false, error: error?.message ?? String(error) });
    });

  return true; // keep the channel open for the async response
});
