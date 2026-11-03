/**
 * "Vendor logins" panel. Runs on an extension page (shown inside the popup and, as an iframe,
 * in the dropdown next to Automate on Zendesk) so Zendesk's own scripts can never read the
 * password field. The password goes straight to the service worker, is stored in
 * chrome.storage.session, and is never rendered back, logged or kept in this page.
 */
import { LOGIN_HOSTS, VENDORS } from "./lib/vendors.js";
import { MESSAGES } from "./lib/messages.js";

const app = document.getElementById("app");
const focusHost = new URLSearchParams(location.search).get("focus");

const send = (message) => new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function renderVendor(host, status) {
  const { label } = VENDORS[host];
  const info = status?.[host] ?? { saved: false, username: "" };

  const chip = el("span", {
    className: `chip ${info.saved ? "chip--ok" : "chip--none"}`,
    textContent: info.saved ? "Saved this session" : "Not saved",
  });
  const user = el("input", { type: "text", placeholder: "Username", autocomplete: "off", value: info.username });
  const pass = el("input", {
    type: "password",
    placeholder: info.saved ? "Password saved (paste to replace)" : "Password",
    autocomplete: "new-password",
  });
  const msg = el("div", { className: "msg" });
  const save = el("button", { type: "button", textContent: "Save" });
  const clear = el("button", { type: "button", className: "secondary", textContent: "Clear", hidden: !info.saved });

  const card = el(
    "div",
    { className: `vendor${host === focusHost ? " vendor--focus" : ""}` },
    el("div", { className: "vendor__head" }, el("span", { className: "vendor__name", textContent: label }), chip),
    user,
    pass,
    el("div", { className: "row" }, save, clear),
    msg
  );

  const show = (text, isError = false) => {
    msg.textContent = text;
    msg.className = isError ? "msg msg--error" : "msg";
  };

  save.addEventListener("click", async () => {
    if (!user.value.trim() || !pass.value) return show("Enter both username and password.", true);
    save.disabled = true;
    const response = await send({ type: MESSAGES.SET_CREDENTIAL, host, username: user.value, password: pass.value });
    pass.value = "";
    save.disabled = false;
    if (response?.ok) render(response.status, host);
    else show("Could not save. Try again.", true);
  });

  clear.addEventListener("click", async () => {
    const response = await send({ type: MESSAGES.CLEAR_CREDENTIAL, host });
    if (response?.ok) render(response.status);
  });

  return { card, show };
}

function render(status, justSavedHost) {
  app.replaceChildren(
    el("p", {
      className: "hint",
      textContent:
        "Copy the portal login from Keeper once per browser session. It is kept in memory only and is cleared when Chrome restarts.",
    })
  );
  for (const host of LOGIN_HOSTS) {
    const { card, show } = renderVendor(host, status);
    app.append(card);
    if (host === justSavedHost) show("Saved. You can run Automate now.");
    if (host === focusHost && !justSavedHost) card.scrollIntoView({ block: "nearest" });
  }
}

send({ type: MESSAGES.CREDENTIAL_STATUS }).then((response) => render(response?.status));
