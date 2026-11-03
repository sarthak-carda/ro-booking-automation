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
  reporting an error instead of a false success if the cart is empty or changed. Mixed guests
  are supported: Adults book "Full price", Children book "Free" (an Infant or any other guest type
  stops with an error rather than guessing; at least one Adult is required).
- Guest names/emails are trimmed before being typed into vendor forms (Aries data can carry
  trailing spaces, which vendor forms reject).

## Vendors behind a login (Panorama Langkawi, OPA Pisa)

These portals use a plain username/password login (no OTP, captcha or confirmation code).

**Saving the login.** Open the **▾** next to *Automate Booking* (or the toolbar popup), pick the
vendor, paste the username and password from Keeper and press Save. It is stored in
`chrome.storage.session`: memory only, never written to disk, gone when Chrome restarts. The
form is an extension page (an iframe in the dropdown), so Zendesk's own scripts can't read it, and
only extension pages may talk to the credential store. Nothing is hardcoded in any script.

**How a run logs in**, in order: (1) already logged in -> nothing to do; (2) **Keeper's browser
autofill**, if it is on, fills (and usually submits) the login form -- the script just waits a few
seconds for it and presses Login if the fields are filled but not submitted; (3) otherwise the login
saved above, handed to the script as `ctx.credentials`; (4) otherwise `LOGIN_NEEDED`. Two new button
states:

| Status | Meaning | What to do |
|---|---|---|
| `LOGIN_NEEDED` | Portal showed its login page, Keeper did not fill it and nothing is saved this session | Save the login in **▾**, run again |
| `LOGIN_FAILED` | A login was submitted and not accepted | Re-enter it in **▾**; the script never retries a wrong password |

Credentials are never logged: scripts don't echo them, and everything leaving the service worker
(console, telemetry, the reply to Zendesk) is scrubbed of the saved username/password first.

**Guest country.** Only OPA needs it (its form has a mandatory country field). Aries has no
nationality field, so the country is taken from, in order: a nationality/country field on the tour's
guest details (if it has one), the ticket's "Payment Card Country" line, then the phone's country
code. The source is reported in the result (`countrySource`). The card and phone are proxies, not
proof of citizenship; the fallback is Italy, flagged in the result.

**Panorama Langkawi** (`agent.panoramalangkawi.com`, opens `/Sales/pos`): adds one cart line per
variant for the tour (table in `lib/known-scripts.js`, from the ops instructions), Sell Unit Price =
Agent Unit Price. **Malaysian vs Foreign rates come straight from Aries' guest types**
(`MALAYSIAN_ADULT`/`CHILD` -> Malaysian rows, `NON_MALAYSIAN_*` -> Foreign rows; a booking can mix
them), so no nationality is guessed. Express Lane is always Malaysian Adult for all pax. Tour 55401's
`GENERAL` guests book Glass Bottom Gondola and its `ADD_ON_SKYGLIDE_ADULT` guests add the SkyGlide
package. It then fills the customer details (email `tickets@headout.com`), checks the cart total
against the booking's Net Price (MYR), and stops **before Checkout**.

Notes from testing against 40+ real past bookings: tour 72775 uses the plain "360 Gondola" package
(the instructions' "360 Matta" package is RM 30 cheaper than Aries' net); and on tour 72609 /
102851 a booking with a child leaves the cart RM 8 above the net price because the Express Lane add-on
only has an Adult row -- the script reports that mismatch instead of continuing silently.

**OPA Pisa** (opens the agency area, not the public shop Aries links to): New Order -> product ->
date -> customer form (pseudo mail, **the guest's country**) -> time slot (exact, else up to 15 min
earlier / 45 min later; if none, it stops and says so) -> Add to basket. It empties a leftover
basket first, then checks the basket total against the booking's Net Price (tours 61416 and 66690
are EUR 1.5/person above the portal price in Aries, which the script accounts for) and that there is
enough prepaid credit, then **clicks Buy once**. Buy only opens a "Confirm order <number>" page
(recorded live: payment-method select, *Empty basket* and *Confirm* buttons); credit is spent when
someone presses **Confirm** there, so the script stops on that page (result `awaiting: "Confirm"`
with the `orderNumber`) and the agent picks the payment method and confirms. A guard refuses a
second Buy in the same tab.

**Dates in other months:** the OPA calendar's month arrows are `calendar-container__btn--right`/`--left`; an earlier build looked for `--r`/`--l`, so any visit date outside the current month failed with "outside the portal's calendar". Fixed 2026-10-29 (verified for 2 Nov and 15 Dec).

**Sales horizon:** the Panorama portal lists no packages for dates beyond roughly Dec 2026 (seen on a 4 Jan 2027 booking); the script then stops with "The portal has no packages on sale for <date>" so book that one by hand later. Dates in the past make the portal pop an alert, which does not happen on real tickets (future dates).

**Bran Castle** (`bc-bc.secure-cdn.eu.accessoticketing.com`, tours 27047, 68504 = Standard Ticket; 82847 = Royal Tour Plus Fast Pass; 27048 = Guided Royal Tour Plus Fast Pass; no login). Recorded live 2026-11-02 on the accesso e-commerce site (an Angular SPA): empties a leftover cart, picks the date (month arrows), clicks *Buy Now* on the product card (matched by title), sets the pax counters (Aries types map to Adult / Child / Senior / Student; a category the product does not have stops with an error), picks the exact time slot (no tolerance in the instructions: if it is not offered the script stops and lists the options), skips the add-ons, leaves "Ordering for a company?" empty, checks the cart (items = guests; total equals the Aries net price when that is in RON), ticks **only the first** checkbox (Terms and Conditions) and clicks Checkout. The accesso Pay page is a cross-origin iframe: the script returns `frameFill`, and the extension then runs `fillFrame()` in that iframe to type the guest name, the pseudo mail and a generic billing address (the castle's own address). It never touches card fields. **First real run (ticket 36551802, 2026-11-02): first name, last name, email and address were filled; city, county and postcode were not**, so the filler now looks each field up afresh, waits up to 5 s for it (it may render late), accepts dropdowns, and no longer lets a section label like "Card Details" hide a field. The iframe's DOM cannot be inspected from outside, so: the result's `contactFrame` lists what it filled/missed (when something is missed the service-worker console also lists every field of the payment frame, without values, so the selectors can be fixed), and anything missed is left to the agent. The run always ends there: it never touches card fields or the Pay button. Aries links: only the portal host is routed to this script, so Aries must return the booking portal for all four tours (today 82847, 68504 and 27048 still return bran-castle.com pages).

**Fixes from live runs (2026-11-02):** Robben Island login -- Flutter only accepts typing in the field that has focus, so the script focuses each field right before typing (before, only the password registered). Bran Castle -- the payment-form filler matched "Email **address**" as the street-address field and overwrote the email; each control is now used by one field only. The Terms tick and Checkout click retry (input, label, wrapper; up to 4 Checkout clicks) until the payment page opens, and the cart is always checked and emptied first.

**Bran Castle payment page (2026-11-03):** the filler now waits 7 s after the fields first appear (the page keeps loading), then picks the **phone country code** and the **billing Country** from their drop-downs before typing: the code is the one that prefixes the booking phone (the number box then gets the national digits only; typing `+49...` into it does not switch the code), and the Country is the guest country from `lib/guest-country.js` (guest field, then payment-card country, then phone). The two lists are custom widgets whose DOM cannot be inspected from outside, so the first live run records what it found in `hdDiag` (`dropdowns`: ok flag, chosen row, a trimmed HTML sample). A drop-down that did not take is listed as missed (`country`, `phone code`) and left to the agent. The street/city/county/postcode stay the castle's own address.

With Country following the guest, the street, city, county and postcode come from a per-country table of well-known public addresses (the page rejects a postcode that does not fit the country). A guest country that is not in the table keeps Romania and the castle address. The `dropdowns.billing` breadcrumb records what was used.

**Troubleshooting breadcrumbs:** the Robben Island and Bran Castle scripts leave step-by-step notes (no credentials, no guest data) in the portal's `localStorage` under the key `hdDiag`, readable from any tab of the same origin (`JSON.parse(localStorage.hdDiag)`).

**Robben Island** (`robbenisland.plankton.mobi`, tours 97254 = International tickets, 101292 = South African tickets). The portal is a Flutter app with no ordinary page DOM, so the script switches on Flutter's accessibility layer and drives the `flt-semantics` nodes (taps, and typing by focusing the field's real `<input>`). Recorded live 2026-11-02 with a test login. It logs in through the portal's own "Welcome Back!" popup (needed for the operator rates): Keeper/browser autofill if it fills the email and password, else the login saved in the ▾ panel, else `LOGIN_NEEDED` (a rejected login is `LOGIN_FAILED`); the Keeper wait is 7 s, as for OPA Pisa and Panorama; the "Welcome back" popup shown after logging in is closed with its Close button. Recorded live: the popup's fields are real `<input autocomplete=email / current-password>`, but an autofill sets only the DOM values and Flutter does not notice, so the script re-enters whatever the fields hold through normal input events. The login typing path is not tested by the author (no credentials are entered in testing), picks the date and the exact time slot (no tolerance), sets the Adult / Child counters of the tour's ticket family, checks the ticket total against the Aries net price (ZAR; 550 per International adult, 400 per South African adult), then fills one guest-info form per ticket (phone country code and next-of-kin number from the booking's phone, country of residence, first/last name from Aries' per-guest details). South African tickets also need each guest's **SA ID number** and **residential province** (Aries' "Identity Document Details" and "State"); if the booking lacks one the form is left open with everything else filled and the run stops with an error. Finally it clicks *Continue to payment* and **stops on the "Confirm Guest Info" dialog**: "Agree & continue" attests the details are correct, so the agent presses it. Verified live on tour 97254 end to end; on 101292 up to the ID number (no ID was entered in testing). Not yet tested: Child tickets, and multi-guest International bookings.

**TOSC** is tied to tour 50832: any other tour on that site stops with an error.

Prado and TOSC are unchanged in what they stop at (before card payment).

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
  known-scripts.js   POC only: the hardcoded vendor scripts (Prado, TOSC, Panorama, OPA)
  vendors.js         Per-vendor settings: start URL, login needed, pseudo-mail rule
  credentials.js     Session-only vendor logins (chrome.storage.session)
  redact.js          Scrubs saved credentials from anything leaving the worker
  autofill.js        Look up (POC) + run the autofill script (userScripts API)
  tabs.js            Tab helpers (create, wait-for-load)
popup.{html,js,css}  Auth status + environment switcher + vendor logins
credentials.{html,js,css}  Vendor-login form (popup + dropdown iframe)
content.{js,css}     Zendesk button + booking-id extraction
```
