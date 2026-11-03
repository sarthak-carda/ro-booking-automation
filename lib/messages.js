/**
 * Message protocol between the popup / content script and the service worker.
 *
 * NOTE: content scripts cannot import ES modules, so `content.js` repeats the
 * `RUN_BOOKING_AUTOFILL` string literal. Keep them in sync.
 */
export const MESSAGES = Object.freeze({
  /** Popup -> worker: is there a valid Headout session for the current env? */
  AUTH_STATUS: "AUTH_STATUS",
  /** Popup -> worker: open the Ory login page. */
  OPEN_LOGIN: "OPEN_LOGIN",
  /** Content -> worker: run the booking -> vendor -> automation pipeline. */
  RUN_BOOKING_AUTOFILL: "RUN_BOOKING_AUTOFILL",
  /**
   * Content -> worker: the agent directly answered whether manual payment
   * completed. No vendor-side confirmation check is performed.
   */
  PAYMENT_DECISION: "PAYMENT_DECISION",
  /** Credentials page -> worker: which vendor logins are saved this session (no passwords). */
  CREDENTIAL_STATUS: "CREDENTIAL_STATUS",
  /** Credentials page -> worker: save a vendor login for this browser session. */
  SET_CREDENTIAL: "SET_CREDENTIAL",
  /** Credentials page -> worker: forget a vendor login. */
  CLEAR_CREDENTIAL: "CLEAR_CREDENTIAL",
});

/**
 * Booking-automation result statuses — mirror of the generated-script contract
 * (`window.HeadoutAutomation.run`). Keep in sync with the generate-automate-script
 * skill and `content.js` (which hardcodes these strings).
 */
export const BOOKING_STATUS = Object.freeze({
  /** Reached the vendor's payment page; the agent must complete payment by hand. */
  PAYMENT_REQUIRED: "PAYMENT_REQUIRED",
  /** Order placed and a confirmation reference was read (post-payment). */
  CONFIRMED: "CONFIRMED",
  /** The script could not complete a step. */
  ERROR: "ERROR",
  /** The portal showed its login page and no login is saved for this browser session. */
  LOGIN_NEEDED: "LOGIN_NEEDED",
  /** A saved login was submitted but the portal did not accept it (wrong/expired password). */
  LOGIN_FAILED: "LOGIN_FAILED",
});

/** Stable error codes returned to the UI so it can show tailored messages. */
export const ERROR_CODES = Object.freeze({
  NOT_AUTHENTICATED: "NOT_AUTHENTICATED",
  USER_SCRIPTS_DISABLED: "USER_SCRIPTS_DISABLED",
  /** Neither the HeadoutAutomation nor the legacy autofill API was registered. */
  AUTOMATION_API_MISSING: "AUTOMATION_API_MISSING",
  /** The vendor needs the ticket's "Pseudo Mail ID" comment and the ticket has none. */
  PSEUDO_EMAIL_MISSING: "PSEUDO_EMAIL_MISSING",
});
