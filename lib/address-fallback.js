/**
 * Fallback guest address for vendor checkout forms that require one.
 *
 * Aries' guestDetails endpoint does not always return an address (most bookings
 * have no reason to collect one), but several vendor checkout forms require a
 * billing/shipping address field to proceed. When real guest details don't
 * include one, fill in a placeholder New York, USA address rather than leaving
 * the vendor script unable to find a value.
 */
const NY_ADDRESSES = [
  { line1: "350 5th Ave", city: "New York", postalCode: "10118", country: "United States" },
  { line1: "30 Rockefeller Plaza", city: "New York", postalCode: "10112", country: "United States" },
  { line1: "11 Wall St", city: "New York", postalCode: "10005", country: "United States" },
  { line1: "200 Central Park West", city: "New York", postalCode: "10024", country: "United States" },
  { line1: "1 World Trade Center", city: "New York", postalCode: "10007", country: "United States" },
];

function isBlank(value) {
  return value === undefined || value === null || String(value).trim() === "";
}

function hasUsableAddress(address) {
  return !!address && !isBlank(address.line1) && !isBlank(address.city) && !isBlank(address.postalCode);
}

/**
 * Mutates nothing — returns a new guestDetails object, with a random NY
 * address filled in only when the real one is missing or incomplete.
 */
export function withAddressFallback(guestDetails) {
  if (hasUsableAddress(guestDetails?.address)) return guestDetails;
  const pick = NY_ADDRESSES[Math.floor(Math.random() * NY_ADDRESSES.length)];
  return { ...guestDetails, address: { ...pick, ...guestDetails?.address } };
}
