// Helpers every page shares: the dashboard, the sign-in page and the embed widget.
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const fmt = (v, d = 0) => v == null ? "—" : Number(v).toFixed(d);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// GET without data, POST JSON with it; never served from the browser cache
const api = (url, data) => fetch(url, data === undefined ? { cache: "no-store" } :
  { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
// Rewrite a block of controls only when it changed, and keep the keyboard focus on the same control:
// the 5 s poll must never throw a keyboard user back to the top of the page
const lastHTML = new WeakMap();
const FOCUSABLE = "a, button, input, select, [tabindex]";
function setHTML(el, html) {
  if (lastHTML.get(el) === html) return;
  lastHTML.set(el, html);
  const a = document.activeElement, at = el.contains(a) ? [...el.querySelectorAll(FOCUSABLE)].indexOf(a) : -1;
  el.innerHTML = html;
  if (at >= 0) el.querySelectorAll(FOCUSABLE)[at]?.focus();
}
// Why a request failed, in words: Fan Control's own error, or the status when the answer is not Fan
// Control's (a reverse proxy's error page, when Fan Control did not answer it)
const failure = async r => (await r.json().catch(() => ({}))).error ||
  (r.status >= 502 && r.status <= 504 ? `Fan Control did not answer the proxy (HTTP ${r.status})` : `HTTP ${r.status}`);
