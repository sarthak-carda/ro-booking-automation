# Booking fixtures

`sample-booking-1.json` / `sample-booking-2.json` approximate the shape
`background.js` builds in the extension:

```js
const booking = { ...bookingDetails, guestDetails };
```

where `bookingDetails` comes from
`GET {ariesBase}/apis/v2/order-fulfillment/booking/{id}` and `guestDetails`
from `GET {ariesBase}/apis/v2/order-fulfillment/booking/{id}/guestDetails`
(see `lib/aries.js` in the extension repo).

**This is a constructed approximation**, not a captured real response — no
live Aries access was available this pass. The fields referenced elsewhere in
the codebase (`bookingId`, `vendorId`, `tourId`, `inventoryDate`,
`agentEmail`/`email`, `agentName`/`name`) are present; guest fields are a
reasonable guess. Replace these two files with real (test-environment)
booking payloads as soon as they're available — nothing else in the pipeline
assumes more than "has a `bookingId`" (see `pipeline/contextBuilder.js`), so
swapping them in requires no code changes.
