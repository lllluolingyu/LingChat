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

const $ = (id) => document.getElementById(id);

const statusPill = $("status-pill");
const statusText = $("status-text");
const connBanner = $("conn-banner");
const confirmEl = $("confirm");
const confirmCmd = $("confirm-cmd");
const confirmAllow = $("confirm-allow");
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
      sessionStorage.setItem("lingchat-token", fromUrl);
      history.replaceState(null, "", location.pathname + (location.hash || ""));
    } catch {
      /* storage unavailable (e.g. blocked): keep the token in the URL */
    }
    return fromUrl;
  }
  try {
    return sessionStorage.getItem("lingchat-token") || "";
  } catch {
    return "";
  }
})();

// fetch() wrapper that attaches the auth token header to every API call.
export function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (AUTH_TOKEN) headers["X-LingChat-Token"] = AUTH_TOKEN;
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

const STATUS_LABELS = {
  connected: "Connected",
  connecting: "Connecting",
  reconnecting: "Reconnecting",
  offline: "Offline",
};

function setStatus(state) {
  statusPill.dataset.state = state;
  statusText.textContent = STATUS_LABELS[state];
  connBanner.hidden = state !== "reconnecting";
  hooks.onConnectedChange(state === "connected");
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
  sock.addEventListener("close", () => {
    if (stale()) return;
    abandonStreaming();
    hideTyping();
    // Queued confirmations died with the socket: their ids belong to the old
    // server-side session, so an answer sent later would just be ignored —
    // don't leave a modal up asking about a command nothing is waiting on.
    resetConfirms();
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
  if (ws && ws.readyState === WebSocket.OPEN) {
    switching = true;
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
let confirmQueue = []; // [{command, id}]

export function showConfirm(command, id) {
  confirmQueue.push({ command, id });
  if (confirmQueue.length === 1) renderConfirm();
}

function renderConfirm() {
  const item = confirmQueue[0];
  if (!item) {
    confirmEl.classList.add("hidden");
    return;
  }
  confirmCmd.textContent = item.command;
  confirmEl.classList.remove("hidden");
  confirmDeny.focus(); // safe default for a stray Enter
}

function answerConfirm(approved) {
  const item = confirmQueue.shift();
  if (item) wsSend({ type: "confirm_response", id: item.id, approved });
  if (confirmQueue.length) {
    renderConfirm();
  } else {
    confirmEl.classList.add("hidden");
    hooks.focusInput();
  }
}

function resetConfirms() {
  confirmQueue = [];
  confirmEl.classList.add("hidden");
}

confirmAllow.addEventListener("click", () => answerConfirm(true));
confirmDeny.addEventListener("click", () => answerConfirm(false));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !confirmEl.classList.contains("hidden")) {
    answerConfirm(false);
  }
});
