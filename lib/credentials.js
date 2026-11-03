/**
 * Vendor portal logins for the current browser session.
 *
 * The agent copies the username/password from Keeper into the extension once per browser
 * session. They live in `chrome.storage.session`: memory only, never written to disk, wiped
 * when Chrome restarts, and (by default) readable only from the service worker and extension
 * pages -- NOT from content scripts, so the Zendesk page can never read them.
 *
 * Nothing in this module (or its callers) may log, echo or return a password. The popup /
 * dropdown get only `credentialStatus()`, which carries the username but never the password.
 */
import { LOGIN_HOSTS } from "./vendors.js";

const KEY = "vendorCredentials";

// Make the access level explicit instead of relying on the default.
chrome.storage.session.setAccessLevel?.({ accessLevel: "TRUSTED_CONTEXTS" }).catch(() => {});

async function readAll() {
  const stored = await chrome.storage.session.get(KEY);
  return stored[KEY] ?? {};
}

function assertLoginHost(host) {
  if (!LOGIN_HOSTS.includes(host)) throw new Error("UNKNOWN_VENDOR");
}

/** @param {string} host @param {{ username: string, password: string }} credential */
export async function setCredential(host, { username, password }) {
  assertLoginHost(host);
  const user = String(username ?? "").trim();
  const pass = String(password ?? "");
  if (!user || !pass) throw new Error("CREDENTIAL_INCOMPLETE");
  const all = await readAll();
  all[host] = { username: user, password: pass };
  await chrome.storage.session.set({ [KEY]: all });
}

/** @param {string} host @returns {Promise<{ username: string, password: string } | null>} */
export async function getCredential(host) {
  return (await readAll())[host] ?? null;
}

/** @param {string} host */
export async function clearCredential(host) {
  assertLoginHost(host);
  const all = await readAll();
  delete all[host];
  await chrome.storage.session.set({ [KEY]: all });
}

/** Safe for the UI: which vendors have a login saved, and the username -- never the password. */
export async function credentialStatus() {
  const all = await readAll();
  return Object.fromEntries(
    LOGIN_HOSTS.map((host) => [host, { saved: Boolean(all[host]), username: all[host]?.username ?? "" }])
  );
}
