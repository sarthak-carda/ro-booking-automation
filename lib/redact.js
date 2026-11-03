/**
 * Defence in depth: scripts must never echo credentials, but a vendor error message, a DEBUG
 * payload or a page title could still contain one. Everything that leaves the service worker
 * (console, telemetry, the reply to the Zendesk page) goes through here first.
 */

const MASK = "[redacted]";
const MIN_LENGTH = 3; // masking a 1-2 character username would mangle ordinary text

/**
 * Deep-copy `value`, replacing every occurrence of a secret inside any string.
 * @template T
 * @param {T} value
 * @param {{ username?: string, password?: string } | null | undefined} credential
 * @returns {T}
 */
export function redactSecrets(value, credential) {
  const secrets = [credential?.password, credential?.username]
    .filter((s) => typeof s === "string" && s.length >= MIN_LENGTH)
    .sort((a, b) => b.length - a.length);
  if (secrets.length === 0) return value;

  const scrub = (v) => {
    if (typeof v === "string") return secrets.reduce((text, secret) => text.split(secret).join(MASK), v);
    if (Array.isArray(v)) return v.map(scrub);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, scrub(x)]));
    return v;
  };
  return scrub(value);
}
