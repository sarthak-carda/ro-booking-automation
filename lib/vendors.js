/**
 * Per-vendor extension settings, keyed by the host of the vendor link Aries returns.
 *
 * `startUrl` overrides the link Aries returns when that link is not where the agent flow
 * begins (confirmed live: OPA Pisa's Aries link is the public /en/tickets/buy/ page, but the
 * agency ordering app is the reserved-area page below).
 *
 * `login: true` marks portals that sit behind a simple username/password login (no OTP, no
 * captcha, no confirmation code). The agent pastes the credentials once per browser session
 * (see lib/credentials.js); scripts for these vendors receive them as `ctx.credentials`.
 *
 * `pseudoEmail: false` marks vendors whose booking instructions give a fixed contact email
 * instead of the ticket's per-booking pseudo mail, so a missing "Pseudo Mail ID" comment must
 * not block the run.
 */
export const VENDORS = Object.freeze({
  "agent.panoramalangkawi.com": Object.freeze({
    label: "Panorama Langkawi",
    startUrl: "https://agent.panoramalangkawi.com/Sales/pos",
    login: true,
    pseudoEmail: false,
  }),
  // Aries must return this portal host for all four Bran Castle tours (27047, 27048, 68504, 82847).
  "bc-bc.secure-cdn.eu.accessoticketing.com": Object.freeze({
    label: "Bran Castle",
    startUrl: "https://bc-bc.secure-cdn.eu.accessoticketing.com/calendarPricingWithImage/Tickets%20-%20EN?l=en-ro",
    login: false,
    pseudoEmail: true,
  }),
  // Flutter web app (driven through its accessibility tree). Login (needed for the operator rates)
  // is the portal's own popup: Keeper/browser autofill, else the saved login, else by hand.
  "robbenisland.plankton.mobi": Object.freeze({
    label: "Robben Island",
    startUrl: "https://robbenisland.plankton.mobi/event/6334e42d-2237-ec11-80db-0cc47a93d288",
    login: true,
    pseudoEmail: false,
  }),
  "www.opapisa.it": Object.freeze({
    label: "OPA Pisa",
    startUrl: "https://www.opapisa.it/en/tickets/reserved-area/init-agenzia/",
    login: true,
    pseudoEmail: true,
  }),
});

/** Hosts that need a stored login, in display order. */
export const LOGIN_HOSTS = Object.freeze(Object.keys(VENDORS).filter((h) => VENDORS[h].login));

/** @param {string} link @returns {string} */
export function hostOf(link) {
  return new URL(link).host.toLowerCase();
}

/** @param {string} link */
export function vendorForLink(link) {
  return VENDORS[hostOf(link)] ?? null;
}

/** Vendors not listed here keep the original behaviour: a pseudo mail is required. */
export function needsPseudoEmail(link) {
  return vendorForLink(link)?.pseudoEmail ?? true;
}
