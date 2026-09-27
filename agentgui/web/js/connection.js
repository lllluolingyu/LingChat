/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// Authenticated transport: the per-launch auth token, the REST wrapper, the
// WebSocket lifecycle (connect/reconnect/switch), the connection status pill,
// and the shell-confirmation queue — confirm state is connection state (each
// pending id belongs to the server-side session on *this* socket), so it lives
// and dies here.
//
// Cross-module needs (who is the current session, what to do with an inbound
// message, refreshing the composer when connectivity flips) are injected via
// initConnection() so this module imports nothing that imports it back.

import { abandonStreaming, hideTyping } from "./thread.js";
import { t } from "./i18n.js";

const $ = (id) => document.getElementById(id);

const statusPill = $("status-pill");
const statusText = $("status-text");
const connBanner = $("conn-banner");
const confirmEl = $("confirm");
const confirmCmd = $("confirm-cmd");
const confirmRunner = $("confirm-runner");
const confirmAllow = $("confirm-allow");
const confirmAllowSession = $("confirm-allow-session");
const confirmDeny = $("confirm-deny");

// Per-launch auth token: taken from the page URL (?token=...) the server
// prints at startup, and sent on every WebSocket handshake and REST call.
// Persisted to sessionStorage so this tab survives reloads after the token
// is scrubbed from the URL — and scrubbed eagerly (only once safely stored)
// so the address bar / history / screenshots don't carry it around.
const AUTH_TOKEN = (() => {
  const fromUrl = new URLSearchParams(location.search).get("token") || "";
  if (fromUrl) {
    try {
      sessionStorage.setItem("agentgui-token", fromUrl);
      history.replaceState(null, "", location.pathname + (location.hash || ""));
    } catch {
      /* storage unavailable (e.g. blocked): keep the token in the URL */
    }
    return fromUrl;
  }
  try {
    return sessionStorage.getItem("agentgui-token") || "";
  } catch {
    return "";
  }
})();

// fetch() wrapper that attaches the auth token header to every API call.
export function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (AUTH_TOKEN) headers["X-AgentGUI-Token"] = AUTH_TOKEN;
  return fetch(path, Object.assign({}, opts, { headers }));
}

let ws = null;
let everConnected = false;
let switching = false; // intentional reconnect to another session
let suppressReconnect = false; // session open in another tab
let reconnectTimer = null;

// Injected by main.js at boot: {getSession, onMessage, onConnectedChange, focusInput}.
let hooks = {
  getSession: () => null,
  onMessage: () => {},
  onConnectedChange: () => {},
  focusInput: () => {},
};

export function initConnection(options) {
  hooks = Object.assign(hooks, options);
  setStatus("connecting");
}

// --- status -------------------------------------------------------------------

const STATUS_KEYS = {
  connected: "conn.connected",
  connecting: "conn.connecting",
  reconnecting: "conn.reconnecting",
  offline: "conn.offline",
};

let statusState = "connecting";

function setStatus(state) {
  statusState = state;
  statusPill.dataset.state = state;
  statusText.textContent = t(STATUS_KEYS[state]);
  connBanner.hidden = state !== "reconnecting";
  hooks.onConnectedChange(state === "connected");
}

// Re-label the pill after a language change without touching the socket.
export function refreshStatusLabel() {
  statusText.textContent = t(STATUS_KEYS[statusState]);
}

export function socketOpen() {
  return ws !== null && ws.readyState === WebSocket.OPEN;
}

export function wsSend(obj) {
  if (!socketOpen()) return false;
  ws.send(JSON.stringify(obj));
  return true;
}

// --- lifecycle ----------------------------------------------------------------

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams();
  const sessionId = hooks.getSession();
  if (!sessionId) { setStatus("offline"); return; }
  if (sessionId) params.set("session", sessionId);
  if (AUTH_TOKEN) params.set("token", AUTH_TOKEN);
  const query = params.toString() ? `?${params.toString()}` : "";
  const sock = new WebSocket(`${proto}://${location.host}/ws${query}`);
  ws = sock;
  // Handlers ignore stale sockets: an abandoned CONNECTING socket may close
  // long after a newer one took over, and must not trigger a reconnect.
  const stale = () => sock !== ws;

  sock.addEventListener("open", () => {
    if (stale()) return;
    everConnected = true;
    setStatus("connected");
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  });
  sock.addEventListener("message", (ev) => {
    if (stale()) return;
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    hooks.onMessage(msg);
  });
  sock.addEventListener("close", (event) => {
    if (stale()) return;
    abandonStreaming();
    hideTyping();
    // Queued confirmations died with the socket: their ids belong to the old
    // server-side session, so an answer sent later would just be ignored —
    // don't leave a modal up asking about a command nothing is waiting on.
    resetConfirms();
    if ([4401, 4403, 4404, 4411].includes(event.code)) suppressReconnect = true;
    if (suppressReconnect) {
      setStatus("offline");
      return;
    }
    if (switching) {
      // Intentional reconnect to another session — no scary banner.
      switching = false;
      setStatus("connecting");
      connect();
      return;
    }
    setStatus(everConnected ? "reconnecting" : "connecting");
    // Auto-reconnect with a small delay; ?session= makes it a resume.
    if (!reconnectTimer) reconnectTimer = setTimeout(connect, 1500);
  });
  sock.addEventListener("error", () => sock.close());
}

export function reconnectNow() {
  suppressReconnect = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (
    ws &&
    (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)
  ) {
    switching = true;
    // A session switch can happen before the initial handshake completes.
    // Close that CONNECTING socket too: replacing it without closing would
    // leave a stale server connection holding the old session attachment.
    ws.close(); // close handler reconnects immediately
  } else {
    switching = false;
    setStatus("connecting");
    connect();
  }
}

// Stop auto-reconnecting (the session is open in another tab); the next
// reconnectNow() — e.g. picking another session — lifts it.
export function suspendReconnect() {
  suppressReconnect = true;
}

// --- confirm modal ---------------------------------------------------------------

// Confirmations are queued: parallel tool calls can request several at once,
// each with its own id, and we resolve them one modal at a time by id so an
// approval is never misrouted to the wrong command.
let confirmQueue = []; // [{command, id, pattern, runner}]

export function showConfirm(request) {
  confirmQueue.push(request);
  if (confirmQueue.length === 1) renderConfirm();
}

function renderConfirm() {
  const item = confirmQueue[0];
  if (!item) { confirmEl.classList.add("hidden"); return; }
  $("confirm-title").textContent = item.title || t("approval.title");
  confirmCmd.textContent = item.detail || "";
  confirmRunner.textContent = item.diff || "";
  confirmRunner.className = "approval-diff";
  confirmRunner.hidden = !item.diff;
  confirmAllowSession.hidden = !item.options?.includes("session");
  confirmAllowSession.textContent = t("approval.allow_session");
  confirmEl.classList.remove("hidden");
  confirmDeny.focus();
}

function answerConfirm(approved, scope = "once") {
  const item = confirmQueue.shift();
  if (item) wsSend({ type: "approval_response", id: item.id, decision: approved ? scope : "deny" });
  renderConfirm();
  if (!confirmQueue.length) hooks.focusInput();
}

export function resetConfirms() {
  confirmQueue = [];
  confirmEl.classList.add("hidden");
}

confirmAllow.addEventListener("click", () => answerConfirm(true));
confirmAllowSession.addEventListener("click", () => answerConfirm(true, "session"));
confirmDeny.addEventListener("click", () => answerConfirm(false));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !confirmEl.classList.contains("hidden")) {
    answerConfirm(false);
  }
});
