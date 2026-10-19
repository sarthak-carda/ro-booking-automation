/** Aries API client: booking details, guest details, and the vendor-tour link. */
import { authenticatedFetch } from "./auth-fetch.js";
import { applyBookingTestOverrides, BOOKING_TEST_OVERRIDES } from "./test-helpers.js";

/**
 * Fetch full booking details.
 * @param {import("../config.js").Environment} env
 * @param {string|number} bookingId
 */
export async function fetchBooking(env, bookingId) {
  const id = encodeURIComponent(bookingId);
  const res = await authenticatedFetch(
    env,
    `${env.ariesBase}/apis/v2/order-fulfillment/booking/${id}`,
    { referer: `${env.ariesBase}/bms/booking/${id}` }
  );
  if (!res.ok) throw new Error(`Booking API returned ${res.status}`);
  
  // For test
  return applyBookingTestOverrides(await res.json(), BOOKING_TEST_OVERRIDES);
  
  // For production
  // return  res.json(); 
}

/**
 * Fetch guest details for a booking.
 *
 * Real finding (2026-10): unlike fetchBooking's endpoint, this one is NOT reachable
 * directly on `ariesBase` — confirmed live (two real bookings, both PENDING/manual-
 * fulfillment TOSC): a direct `GET {ariesBase}/apis/v2/order-fulfillment/booking/{id}/
 * guestDetails` returns a genuine 404 (Aries' own branded 404 page, not an auth/CORS
 * failure), while the real BMS page for the exact same booking successfully calls the
 * SAME path through BMS's own reverse proxy instead: `{boxOfficeBase}/proxy/aries/<same
 * path>` (watched BMS's real network traffic to confirm this — 200, real guest data).
 * fetchBooking's own endpoint keeps working fine directly on ariesBase; only this
 * guestDetails sub-resource needs the proxy.
 * @param {import("../config.js").Environment} env
 * @param {string|number} bookingId
 */
export async function fetchGuestDetails(env, bookingId) {
  const id = encodeURIComponent(bookingId);
  const res = await authenticatedFetch(
    env,
    `${env.boxOfficeBase}/proxy/aries/apis/v2/order-fulfillment/booking/${id}/guestDetails`,
    { referer: `${env.boxOfficeBase}/bms/${id}` }
  );
  if (!res.ok) throw new Error(`Guest details API returned ${res.status}`);

  return res.json();
}

/**
 * Resolve the vendor link for a booking via the vendor-tour endpoint.
 * @param {import("../config.js").Environment} env
 * @param {{ bookingId: number, vendorId: number, tourId: number }} booking
 * @returns {Promise<string>} An absolute URL.
 */
export async function resolveVendorLink(env, booking) {
  const { bookingId, vendorId, tourId } = booking;
  if (vendorId == null || tourId == null) {
    throw new Error("Booking response is missing vendorId/tourId");
  }

  const query = new URLSearchParams({ vendorId, tourId });
  const res = await authenticatedFetch(
    env,
    `${env.ariesBase}/apis/vendor-tour?${query}`,
    { referer: `${env.ariesBase}/bms/booking/${encodeURIComponent(bookingId)}` }
  );
  if (!res.ok) throw new Error(`Vendor-tour API returned ${res.status}`);

  const link = extractVendorLink(await res.json());
  if (!link) throw new Error("No link found in vendor-tour response");
  return toAbsoluteUrl(link);
}

/**
 * Find the most likely link in an arbitrary vendor-tour response by walking it
 * and preferring keys named like link/url/host (and URL-looking values).
 */
export function extractVendorLink(data) {
  const candidates = [];

  const visit = (value, key) => {
    if (value == null) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      const priority = keyPriority(key);
      if (trimmed && priority > 0) {
        candidates.push({ value: trimmed, priority, isUrl: looksLikeUrl(trimmed) });
      }
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, key));
    } else if (typeof value === "object") {
      for (const [k, v] of Object.entries(value)) visit(v, k);
    }
  };

  visit(data, "");
  candidates.sort((a, b) => b.priority - a.priority || Number(b.isUrl) - Number(a.isUrl));
  return candidates[0]?.value ?? null;
}

function keyPriority(key) {
  const k = String(key).toLowerCase();
  if (k === "link") return 5;
  if (k.includes("link")) return 4;
  if (k === "url") return 3;
  if (k.includes("url")) return 2;
  if (k.includes("host")) return 1;
  return 0;
}

function looksLikeUrl(value) {
  return /^https?:\/\//i.test(value) || /\.[a-z]{2,}\//i.test(value);
}

function toAbsoluteUrl(link) {
  return /^https?:\/\//i.test(link) ? link : `https://${link.replace(/^\/+/, "")}`;
}
