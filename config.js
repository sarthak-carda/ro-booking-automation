/**
 * Static configuration: Headout environments and the local autofill service.
 *
 * Auth is cookie-based and shared across `*.headout.com`, so each environment is
 * identified by the registrable domain its Ory session cookie is scoped to.
 */

/**
 * @typedef {Object} Environment
 * @property {string} key          Stable identifier (matches the ENVIRONMENTS key).
 * @property {string} label        Human-readable name.
 * @property {string} oryBase      Ory auth/login origin.
 * @property {string} ariesBase    Aries API origin.
 * @property {string} boxOfficeBase BMS (box-office) origin — some Aries sub-resources
 *   (confirmed: guestDetails) are not reachable directly on ariesBase and must be called
 *   through BMS's own reverse proxy at `${boxOfficeBase}/proxy/aries/<same path>` instead.
 *   Found 2026-10 by watching the real BMS page's own network traffic for a booking whose
 *   direct `ariesBase` guestDetails call 404'd: BMS successfully calls
 *   `box-office.headout.com/proxy/aries/apis/v2/order-fulfillment/booking/{id}/guestDetails`
 *   (200) for the exact same path direct-to-Aries 404s on. See lib/aries.js.
 * @property {string} eos          EOS app origin (used as the login `return_to`).
 * @property {string} cookieDomain Registrable domain the session cookie lives on.
 */

/** @type {Record<string, Omit<Environment, "key">>} */
export const ENVIRONMENTS = {
  prod: {
    label: "Production",
    oryBase: "https://auth.headout.com",
    ariesBase: "https://aries.headout.com",
    boxOfficeBase: "https://box-office.headout.com",
    eos: "https://eos.headout.com",
    cookieDomain: "headout.com",
  },
  test: {
    label: "Test / Staging",
    oryBase: "https://auth.test-headout.com",
    ariesBase: "https://aries.test-headout.com",
    // Unverified: mirrors the prod host's naming pattern, but unlike the other test-env
    // hosts above, this one hasn't actually been confirmed to exist/respond the same way.
    boxOfficeBase: "https://box-office.test-headout.com",
    eos: "https://eos.test-headout.com",
    cookieDomain: "test-headout.com",
  },
};

export const DEFAULT_ENVIRONMENT_KEY = "prod";

/** Automate server base URL. */
export const SERVER_BASE = "http://127.0.0.1:3000";

/** Script resolve endpoint — returns JS for a vendor URL. */
export const AUTOFILL_SERVICE = {
  scriptUrl: `${SERVER_BASE}/api/scripts/resolve`,
  /** Query parameter used to pass the vendor link when fetching the script. */
  linkParam: "url",
};

const STORAGE_KEY = "selectedEnv";

/** Resolve the environment the user selected in the popup (defaults to prod). */
export async function getEnvironment() {
  const { [STORAGE_KEY]: key } = await chrome.storage.local.get(STORAGE_KEY);
  const resolvedKey = ENVIRONMENTS[key] ? key : DEFAULT_ENVIRONMENT_KEY;
  return { key: resolvedKey, ...ENVIRONMENTS[resolvedKey] };
}

/** Persist the selected environment. */
export function setEnvironment(key) {
  return chrome.storage.local.set({ [STORAGE_KEY]: key });
}
