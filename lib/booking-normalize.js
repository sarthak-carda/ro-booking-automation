/**
 * Normalizes Aries' real (nested, inconsistent) booking/guestDetails API shapes into the
 * flat `{ firstName, lastName, email, phone, address, totalPax }` shape every vendor
 * automation script actually reads via `getByPath(booking, "guestDetails.firstName")` etc.
 *
 * Real bug found 2026-10 (confirmed live against two real bookings, two different
 * vendors): background.js used to pass Aries' raw guestDetails response straight through
 * (`{ ...bookingDetails, guestDetails }`, no transformation at all). The real response has
 * NO top-level firstName/lastName/email/phone/address, and no flat totalPax on the booking
 * either:
 *
 *   guestDetails = {
 *     primaryGuest: { firstName, lastName, email, bookingUserFields: [...] },
 *     paxDetails: [{ count, paxType }],
 *     guests: [...], guestCustomFields: [...], ...
 *   }
 *   booking = { ..., guestNumbers: [{ type: "ADULT", persons: N }, ...], ... }  // no totalPax
 *
 * So `getByPath(booking, "guestDetails.firstName")` resolved to `undefined` on every REAL
 * ticket run, and `getByPath(booking, "totalPax") || 1` silently fell back to 1 regardless
 * of the real guest count -- both scripts still reached PAYMENT_REQUIRED anyway, because
 * that only requires finding and filling form fields, not filling them with correct values.
 */

/**
 * @param {unknown} rawGuestDetails - Aries' raw guestDetails response (see module docstring)
 * @returns {{ firstName: string, lastName: string, email: string, phone: string,
 *   address: { line1: string, city: string, postalCode: string, country: string } }}
 */
export function normalizeGuestDetails(rawGuestDetails) {
  const primary = rawGuestDetails?.primaryGuest ?? rawGuestDetails?.guests?.[0] ?? {};
  const bookingUserFields = primary.bookingUserFields ?? rawGuestDetails?.guests?.[0]?.bookingUserFields ?? [];

  const fieldValue = (fieldTypeName) =>
    bookingUserFields.find((f) => f.tourUserFieldType?.name === fieldTypeName || f.name === fieldTypeName)?.value;

  return {
    firstName: cleanText(primary.firstName),
    lastName: cleanText(primary.lastName),
    email: cleanText(primary.email),
    phone: cleanText(fieldValue("PHONE")),
    // Aries' guestDetails never includes a street address for any vendor we've seen --
    // confirmed live, not just absent for this one booking. Vendor forms that require one
    // (Prado) still need a value to proceed, so fall back to a random NY, USA address --
    // never a real guest address, since Aries has none to give us.
    address: withAddressFallback(),
  };
}

/**
 * Real bug found 2026-10-08 (RO team, live TOSC ticket): Aries returned a last name of
 * "Muck " -- trailing space -- which the vendor form rejects ("The format is invalid").
 * Aries guest fields are free text; strip zero-width characters, collapse whitespace, trim.
 */
function cleanText(value) {
  return String(value ?? "")
    .normalize("NFC")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

const NY_ADDRESSES = [
  { line1: "350 5th Ave", city: "New York", postalCode: "10118", country: "United States" },
  { line1: "30 Rockefeller Plaza", city: "New York", postalCode: "10112", country: "United States" },
  { line1: "11 Wall St", city: "New York", postalCode: "10005", country: "United States" },
  { line1: "200 Central Park West", city: "New York", postalCode: "10024", country: "United States" },
  { line1: "1 World Trade Center", city: "New York", postalCode: "10007", country: "United States" },
];

function withAddressFallback() {
  return NY_ADDRESSES[Math.floor(Math.random() * NY_ADDRESSES.length)];
}

/**
 * @param {unknown} rawBooking - Aries' raw booking response (see module docstring)
 * @returns {number} total pax across every guestNumbers entry (ADULT + CHILD + ...),
 *   defaulting to 1 only if the booking genuinely carries no guest-count data at all.
 */
export function computeTotalPax(rawBooking) {
  const guestNumbers = rawBooking?.guestNumbers;
  if (!Array.isArray(guestNumbers) || guestNumbers.length === 0) return 1;
  const total = guestNumbers.reduce((sum, g) => sum + (Number(g?.persons) || 0), 0);
  return total > 0 ? total : 1;
}
