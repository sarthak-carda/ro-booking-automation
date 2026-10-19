/** Test-only booking payload transforms. */

export const BOOKING_TEST_OVERRIDES = {
  enabled: false,
  date: "2026-07-19",
  dateKeys: [
    "inventoryDate",
  ],
};

/**
 * Apply configured local test overrides to an Aries booking payload.
 * @param {unknown} booking
 * @param {{ enabled?: boolean, date?: string, dateKeys?: string[] }} options
 * @returns {unknown}
 */
export function applyBookingTestOverrides(booking, options = {}) {
  if (!options.enabled) return booking;
  return overrideBookingDates(booking, {
    date: options.date,
    dateKeys: new Set(options.dateKeys ?? []),
  });
}

function overrideBookingDates(value, { date, dateKeys }) {
  if (!date || dateKeys.size === 0) return value;
  if (Array.isArray(value)) return value.map((item) => overrideBookingDates(item, { date, dateKeys }));
  if (!value || typeof value !== "object") return value;

  return Object.fromEntries(
    Object.entries(value).map(([key, child]) => [
      key,
      shouldOverrideDate(key, child, dateKeys) ? formatDateLike(child, date) : overrideBookingDates(child, { date, dateKeys }),
    ])
  );
}

function shouldOverrideDate(key, value, dateKeys) {
  if (!dateKeys.has(key)) return false;
  return typeof value === "string" || typeof value === "number" || value instanceof Date;
}

function formatDateLike(value, date) {
  const iso = `${date}T00:00:00.000Z`;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return date;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) return iso;
  if (typeof value === "number") return new Date(iso).getTime();
  if (value instanceof Date) return new Date(iso);
  return date;
}
