import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function AuthScreen() {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event) => {
    event.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    try { await controller.setToken(token.trim()); }
    finally { setBusy(false); }
  };
  return html`<main class="auth-screen"><form class="auth-panel" onSubmit=${submit}>
    <div class="auth-mark">Y</div><h1>Connect to Yoke</h1><p>Enter the bearer token printed by <code>yoke serve</code>. It is kept only for this browser session.</p>
    <label>Bearer token<input type="password" autocomplete="off" autofocus value=${token} onInput=${(event) => setToken(event.currentTarget.value)} /></label>
    <button class="primary" disabled=${busy || !token.trim()}>${busy ? "Connecting…" : "Connect"}</button>
  </form></main>`;
}
