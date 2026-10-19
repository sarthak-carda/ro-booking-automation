# Headout Vendor Autofill — POC build (Chrome MV3)

**This is a standalone POC variant, not the full extension.** It only works for two
vendors — **Prado Museum** (entradas.museodelprado.es) and **TOSC / midaticket.com**
(vive.midaticket.com) — and does not require running `automate-server` at all. The
two vendor automation scripts are hardcoded in `lib/known-scripts.js`, pulled
byte-for-byte from `automate-server`'s real, already-shipped `script_registry.json`
(version 1 for both). Any other vendor will show a clear "no script for this vendor"
error instead of silently doing nothing.

Everything else is unchanged from the real extension: this still looks up the booking
in Aries and autofills the vendor's order form, reusing the agent's existing Headout
login — **a colleague installing this still needs to be logged into Headout's
internal tools (the same `*.headout.com` Ory session) in the same Chrome profile**;
nothing about that requirement goes away in this build.

## Flow

1. A button is injected next to the **Task #** label on `headout.zendesk.com/agent/*`.
2. On click, the content script reads the **Booking id** from the active ticket
   (an explicit `Booking Id: <n>`, or failing that the id in a bare
   `box-office.headout.com/bms/<n>` link) and the **pseudo email** from the ticket's
   internal comments (`Pseudo Mail ID: name_<n>@veritt.com` — veritt, volitand or a
   gmail pseudo address). The pseudo email is what gets typed into the vendor form
   instead of the guest's real email; if the ticket has no `Pseudo Mail ID` comment
   the button shows "Pseudo mail not found in ticket comments" and does nothing.
   It then asks the service worker to run the pipeline:
   1. `fetchBooking` — `GET /apis/v2/order-fulfillment/booking/{id}` (Aries).
   2. `fetchGuestDetails` — `GET /booking/{id}/guestDetails` (Aries), added as
      `booking.guestDetails` before the automation runs.
   3. `resolveVendorLink` — `GET /apis/vendor-tour?vendorId=&tourId=`, then extract
      the link from the response.
   4. `fetchAutofillScript` — **POC build:** a local lookup in
      `lib/known-scripts.js` by the vendor link's host, no network call (the real
      extension instead calls `automate-server`'s script-resolve endpoint here).
   5. Open the vendor link, then run the script with the booking data via the
      `userScripts` API.

## What the vendor scripts do about time and errors

- **Prado** books the booking's exact time slot. If it is sold out (or lacks capacity for the
  pax count) it takes the nearest slot up to 45 minutes **earlier** (the vendor-approved
  tolerance in the ticket's booking instructions); otherwise it stops with an error that lists
  the open slots -- it never books an unrelated time. Works in both the English and Spanish site
  (fresh/incognito sessions are Spanish).
- **TOSC** is open-timed (one "9:30 AM - 7:00 PM" option per day). It waits for the cart to
  update before continuing and checks that the checkout summary lists the booked tickets,
  reporting an error instead of a false success if the cart is empty or changed.
- Guest names/emails are trimmed before being typed into vendor forms (Aries data can carry
  trailing spaces, which vendor forms reject).

## Authentication

Auth reuses the browser's shared `.headout.com` Ory session — no tokens stored.
Because the extension origin is a different site (SameSite), the service worker
reads the full cookie set via `chrome.cookies` and injects it (plus a same-origin
`Referer`) using `declarativeNetRequest`. `Cookie`/`Referer` are forbidden headers
for `fetch`, which is why this goes through DNR. See `lib/auth-fetch.js`.

## Setup

No `automate-server`, no account, nothing to run locally beyond the browser itself:

1. `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select this folder.
3. On the extension's **Details** page, turn on **"Allow user scripts"**
   (required to execute the autofill script).
4. Log into Headout's internal tools normally, in this **same** Chrome profile
   (the extension reuses that login — see Authentication above; there's no
   separate sign-in inside the extension itself).

That's it — try it on a real Zendesk ticket for a Prado or TOSC/midaticket.com
booking. Any other vendor will show a "no script for this vendor" error; that's
expected for this POC build, not a bug.

After editing files, click **reload ↻** on the extension card and refresh any open
Zendesk tab so the content script reloads.

## Layout

```
manifest.json        MV3 manifest, permissions, content-script registration
config.js            Environments (prod/test) + autofill service config
background.js        Service worker: message router + pipeline orchestration
lib/
  messages.js        Message protocol + error codes
  session.js         Ory session detection (login state)
  auth-fetch.js      Authenticated fetch (cookie/referer injection via DNR)
  aries.js           Aries client: booking, guest details, vendor link
  known-scripts.js   POC only: the two hardcoded vendor scripts (Prado, TOSC)
  autofill.js        Look up (POC) + run the autofill script (userScripts API)
  tabs.js            Tab helpers (create, wait-for-load)
popup.{html,js,css}  Auth status + environment switcher
content.{js,css}     Zendesk button + booking-id extraction
```
