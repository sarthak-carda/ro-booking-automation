"""Stage 0 — Context builder.

Ports src/pipeline/contextBuilder.js exactly. Assembles the per-vendor
"generation request" the rest of the pipeline consumes: the vendor's portal
URL + login requirement, and a sample booking shaped exactly like the
extension's own `fetchBooking` + `fetchGuestDetails` combined output (see
../../../lib/aries.js in the extension repo — this mirrors that shape, with
`guestDetails` merged in the same way `background.js` does before invoking
the automation script).
"""
from typing import Any, Optional


def _coalesce(*values: Any) -> Any:
    """Mirrors JS `??` (nullish coalescing) — falls through on None only,
    unlike Python's `or` which also falls through on falsy-but-not-None
    values (0, "", False)."""
    for value in values:
        if value is not None:
            return value
    return None


def build_generation_request(vendor: Optional[dict], sample_booking: Optional[dict]) -> dict:
    """@param vendor - { id, name, portalUrl, requiresCredentials, fulfillmentType }
    @param sample_booking - a booking payload already shaped like fetchBooking()'s
      response, with `guestDetails` merged in (matches what the script's `run(booking)`
      receives)
    @returns generation request dict
    """
    if not vendor or not vendor.get("portalUrl"):
        raise ValueError("Vendor is missing portalUrl")
    if not sample_booking or not sample_booking.get("bookingId"):
        raise ValueError("Sample booking is missing bookingId")

    return {
        "vendorId": vendor.get("id"),
        "vendorName": vendor.get("name"),
        "portalUrl": vendor.get("portalUrl"),
        "requiresCredentials": bool(vendor.get("requiresCredentials")),
        "fulfillmentType": _coalesce(vendor.get("fulfillmentType"), sample_booking.get("fulfillmentType"), "unknown"),
        "bookingSchemaSample": sample_booking,
    }
