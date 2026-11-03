/**
 * Which country is the guest from? Needed by vendors that price or require it (Panorama:
 * Malaysian vs Foreign rates; OPA Pisa: a mandatory country field).
 *
 * Aries has NO dedicated nationality field in guest details (confirmed live: per guest it carries
 * only name / phone / email custom fields). So we use the best signal available, in order:
 *   1. a guest field that is explicitly about nationality / citizenship / country / residence,
 *      if the tour collects one;
 *   2. the ticket's "Payment Card Country" line (read from the Zendesk ticket by the content script);
 *   3. the phone number's country calling code.
 * The signal used is returned in `source` and surfaced in the run result, because 2 and 3 are
 * proxies, not proof of citizenship.
 */

// "Name of country in English" -> ISO 3166-1 alpha-2, built from the platform's own region names.
let isoByName;
function buildIsoByName() {
  const names = new Intl.DisplayNames(["en"], { type: "region" });
  const map = new Map();
  for (let a = 65; a <= 90; a++) {
    for (let b = 65; b <= 90; b++) {
      const code = String.fromCharCode(a, b);
      let name;
      try { name = names.of(code); } catch { continue; }
      // First code wins: 'UK' is a reserved alias that Intl also names "United Kingdom"; GB (earlier) is the real code.
      if (name && name !== code && !map.has(name.toLowerCase())) map.set(name.toLowerCase(), code);
    }
  }
  // Names people actually write that Intl spells differently.
  for (const [alias, code] of Object.entries({
    usa: "US", "united states of america": "US", uk: "GB", england: "GB", scotland: "GB", wales: "GB",
    "great britain": "GB", "south korea": "KR", "czech republic": "CZ", russia: "RU", turkey: "TR",
    vietnam: "VN", "viet nam": "VN", uae: "AE", "ivory coast": "CI",
  })) map.set(alias, code);
  return map;
}

/** English country name for an ISO code, or "" if unknown. */
export function countryName(iso) {
  if (!/^[A-Z]{2}$/.test(iso ?? "")) return "";
  try {
    const name = new Intl.DisplayNames(["en"], { type: "region" }).of(iso);
    return name && name !== iso ? name : "";
  } catch {
    return "";
  }
}

/** ISO code for free text ("Malaysia", "malaysian", "MY"), or "" if it isn't recognisable. */
export function isoFromText(text) {
  const t = String(text ?? "").trim();
  if (!t) return "";
  if (/^[A-Za-z]{2}$/.test(t)) return t.toUpperCase();
  if (/malaysia/i.test(t)) return "MY"; // covers "Malaysian" too
  isoByName ??= buildIsoByName();
  return isoByName.get(t.toLowerCase()) ?? "";
}

// Country calling codes -> ISO (longest prefix wins). Shared codes map to their biggest user
// (+1 -> US, +7 -> RU); that is a heuristic of last resort.
const CALLING_CODES = {
  1: "US", 7: "RU", 20: "EG", 27: "ZA", 30: "GR", 31: "NL", 32: "BE", 33: "FR", 34: "ES", 36: "HU", 39: "IT",
  40: "RO", 41: "CH", 43: "AT", 44: "GB", 45: "DK", 46: "SE", 47: "NO", 48: "PL", 49: "DE", 51: "PE", 52: "MX",
  53: "CU", 54: "AR", 55: "BR", 56: "CL", 57: "CO", 58: "VE", 60: "MY", 61: "AU", 62: "ID", 63: "PH", 64: "NZ",
  65: "SG", 66: "TH", 81: "JP", 82: "KR", 84: "VN", 86: "CN", 90: "TR", 91: "IN", 92: "PK", 93: "AF", 94: "LK",
  95: "MM", 98: "IR", 212: "MA", 213: "DZ", 216: "TN", 234: "NG", 254: "KE", 351: "PT", 352: "LU", 353: "IE",
  354: "IS", 355: "AL", 356: "MT", 357: "CY", 358: "FI", 359: "BG", 370: "LT", 371: "LV", 372: "EE", 380: "UA",
  381: "RS", 385: "HR", 386: "SI", 420: "CZ", 421: "SK", 852: "HK", 853: "MO", 855: "KH", 856: "LA", 880: "BD",
  886: "TW", 960: "MV", 961: "LB", 962: "JO", 966: "SA", 971: "AE", 972: "IL", 974: "QA", 975: "BT", 977: "NP",
};

/** ISO code from a phone number with a country prefix ("+60 12…" / "0060…"), or "". */
export function isoFromPhone(phone) {
  const digits = String(phone ?? "").trim().replace(/^00/, "+");
  if (!digits.startsWith("+")) return "";
  const number = digits.slice(1).replace(/\D/g, "");
  for (const len of [3, 2, 1]) {
    const iso = CALLING_CODES[Number(number.slice(0, len))];
    if (iso && number.slice(0, len).length === len) return iso;
  }
  return "";
}

/** Every named guest field in the raw Aries guest-details response, flattened. */
function guestFields(rawGuestDetails) {
  const fields = [];
  const push = (name, value) => {
    if (name && typeof value === "string" && value.trim()) fields.push({ name: String(name), value: value.trim() });
  };
  for (const guest of [rawGuestDetails?.primaryGuest, ...(rawGuestDetails?.guests ?? [])]) {
    for (const f of guest?.bookingUserFields ?? []) push(f.tourUserFieldType?.name ?? f.name, f.value);
  }
  for (const entry of rawGuestDetails?.guestCustomFields ?? []) {
    for (const f of entry?.customFields ?? []) push(f.fieldName, f.value);
  }
  return fields;
}

/**
 * @param {unknown} rawGuestDetails Aries guest-details response
 * @param {{ phone?: string, cardCountry?: string }} [hints]
 * @returns {{ iso: string, name: string, source: "guest-field"|"card"|"phone"|"none", isMalaysian: boolean }}
 */
export function resolveGuestCountry(rawGuestDetails, { phone, cardCountry } = {}) {
  let iso = "";
  let source = "none";

  const explicit = guestFields(rawGuestDetails).find((f) => /nationalit|citizen|countr|residen/i.test(f.name));
  if (explicit && (iso = isoFromText(explicit.value))) source = "guest-field";
  else if ((iso = isoFromText(cardCountry))) source = "card";
  else if ((iso = isoFromPhone(phone))) source = "phone";

  if (!iso) source = "none";
  return { iso, name: countryName(iso), source, isMalaysian: iso === "MY" };
}
