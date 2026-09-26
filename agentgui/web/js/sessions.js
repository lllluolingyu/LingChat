/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// Session sidebar: the stored-session list, switch/rename/delete, transcript
// replay, the chat title, and the mobile drawer. Owns the current session id
// — the URL hash holds it across reloads, the server's "hello" is
// authoritative for what this socket actually attached to.

import { api, reconnectNow } from "./connection.js";
import {
  addAgentMarkdown,
  addNote,
  addToolCard,
  addUserMessage,
  clearThread,
  appendThinking,
  renderUsage,
  resetPendingTools,
  resolveToolCard,
  scrollToBottom,
  showEmptyState,
} from "./thread.js";

const $ = (id) => document.getElementById(id);

const sessionListEl = $("session-list");
const chatTitleEl = $("chat-title");
const menuBtn = $("menu-btn");
const sidebarClose = $("sidebar-close");
const backdrop = $("backdrop");

let sessionId = (location.hash || "").replace(/^#/, "") || null;
let pendingForkEdit = null; // {session, seq, text}; consumed after fork reconnect

// Monotonic guard for async thread loads: each loadThread() invalidates every
// older in-flight history fetch, so a slow response for session A can never
// paint into session B's just-cleared thread.
let threadGen = 0;

export function getSessionId() {
  return sessionId;
}

export function takePendingForkEdit(id) {
  if (!pendingForkEdit || pendingForkEdit.session !== id) return null;
  const pending = pendingForkEdit;
  pendingForkEdit = null;
  return pending;
}

// Adopt the server-confirmed session id (from "hello") and mirror it into the
// URL hash so a reload resumes the same conversation.
export function adoptSession(id) {
  sessionId = id || null;
  if (sessionId) history.replaceState(null, "", `#${sessionId}`);
  else history.replaceState(null, "", location.pathname);
}

export function setChatTitle(title) {
  const t = title || "New chat";
  chatTitleEl.textContent = t;
  chatTitleEl.title = t;
  document.title = title ? `${title} · Agent Chat` : "Agent Chat";
}

function relTime(iso) {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// --- listing ---------------------------------------------------------------

export async function refreshSessions() {
  let data;
  try {
    data = await (await api("/api/sessions")).json();
  } catch {
    return null;
  }
  if (!data.enabled) {
    renderSessionsDisabled(data.notice);
    return data;
  }
  renderSessionList(data.sessions);
  const current = data.sessions.find((s) => s.id === sessionId);
  if (current) setChatTitle(current.title);
  return data;
}

// Decide between stored history and the empty state without probing a
// fresh id (a transcript GET for a never-spoken session is a guaranteed
// 404 — the sidebar list we need anyway already knows the answer).
export async function loadThread() {
  const gen = ++threadGen;
  const data = await refreshSessions();
  if (gen !== threadGen) return; // a newer load superseded this one
  if (!data) {
    // Listing failed (transient?) — fall back to probing directly.
    if (sessionId) await fetchHistory(sessionId, gen);
    else showEmptyState();
    return;
  }
  const known =
    data.enabled && sessionId && data.sessions.some((s) => s.id === sessionId);
  if (known) await fetchHistory(sessionId, gen);
  else showEmptyState();
}

function renderSessionsDisabled(notice) {
  sessionListEl.textContent = "";
  const note = document.createElement("div");
  note.className = "sidebar-note";
  note.textContent =
    notice || "session history is off for this profile (sessions.enabled: false)";
  sessionListEl.appendChild(note);
}

function dateGroup(iso) {
  const d = new Date(iso);
  const now = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

function renderSessionList(sessions) {
  sessionListEl.textContent = "";
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "sidebar-empty";
    empty.textContent = "No conversations yet";
    sessionListEl.appendChild(empty);
    return;
  }
  let group = null;
  for (const s of sessions) {
    const g = dateGroup(s.updated_at);
    if (g !== group) {
      group = g;
      const label = document.createElement("div");
      label.className = "session-group";
      label.textContent = g;
      sessionListEl.appendChild(label);
    }
    sessionListEl.appendChild(sessionItem(s));
  }
}

function sessionItem(s) {
  const item = document.createElement("div");
  item.className = "session-item" + (s.id === sessionId ? " active" : "");

  const title = document.createElement("div");
  title.className = "session-title";
  title.textContent = s.title || "New chat";

  const badge = document.createElement("span");
  badge.className = "backend-badge";
  badge.textContent = s.backend || "agent";
  title.prepend(badge);

  const time = document.createElement("div");
  time.className = "session-time";
  time.textContent = `${relTime(s.updated_at)} · ${s.message_count} msgs`;

  const actions = document.createElement("span");
  actions.className = "session-actions";

  const rename = document.createElement("button");
  rename.className = "icon-btn";
  rename.title = "Rename";
  rename.innerHTML =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9.8 3.1l3.1 3.1L6 13.1l-3.6.5.5-3.6zM11.6 1.3l3.1 3.1" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  rename.addEventListener("click", (e) => {
    e.stopPropagation();
    startRename(item, title, s);
  });

  const del = document.createElement("button");
  del.className = "icon-btn danger";
  del.title = "Delete";
  del.innerHTML =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.8 4.2h10.4M6.2 4V2.8h3.6V4M4 4.2l.7 9a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9l.7-9M6.5 7v4.4M9.5 7v4.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!item.classList.contains("confirm-delete")) {
      item.classList.add("confirm-delete");
      title.textContent = "Delete this chat?";
      del.title = "Click again to confirm";
      setTimeout(() => {
        if (item.isConnected && item.classList.contains("confirm-delete")) {
          item.classList.remove("confirm-delete");
          title.textContent = s.title || "New chat";
          del.title = "Delete";
        }
      }, 3200);
      return;
    }
    deleteSession(s.id);
  });

  actions.append(rename, del);
  item.append(title, time, actions);
  item.addEventListener("click", () => {
    closeSidebar();
    switchSession(s.id);
  });
  return item;
}

function startRename(item, titleEl, s) {
  if (item.querySelector(".session-rename-input")) return;
  const input = document.createElement("input");
  input.className = "session-rename-input";
  input.value = s.title || "";
  input.placeholder = "Session name";
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    input.replaceWith(titleEl);
    if (save && value && value !== s.title) renameSession(s.id, value);
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

async function renameSession(id, title) {
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
  } catch {
    return;
  }
  refreshSessions();
}

// --- transcript replay -------------------------------------------------------

async function fetchHistory(id, gen) {
  let res;
  try {
    res = await api(`/api/sessions/${encodeURIComponent(id)}`);
  } catch {
    if (gen === threadGen) showEmptyState();
    return;
  }
  if (gen !== threadGen) return; // the thread moved on while we waited
  if (!res.ok) {
    // 404: fresh id, nothing stored yet.
    showEmptyState();
    return;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    return;
  }
  if (gen !== threadGen) return;
  renderHistory(data.turns || []);
  if (data.title) setChatTitle(data.title);
}

function renderHistory(turns) {
  clearThread();
  for (const turn of turns) {
    let text = "";
    let sawText = false;
    const flush = (seq = null) => {
      if (text) { addAgentMarkdown(text, seq); text = ""; }
    };
    for (const frame of turn.frames) {
      switch (frame.type) {
        case "user":
          flush(); addUserMessage(frame.text, frame.attachments || [], Boolean(frame.name), turn.seq); break;
        case "text": text += frame.delta || ""; sawText = true; break;
        case "thinking": flush(); appendThinking(frame.delta || ""); break;
        case "tool_call":
          flush(); addToolCard(frame.id, frame.name, frame.arguments); break;
        case "tool_result":
          flush(); resolveToolCard(frame.id, frame.name, frame.ok, frame.content || "", frame.attachments || [], frame.diff); break;
        case "final": if (!sawText && frame.content) text = frame.content; break;
        case "usage": renderUsage(frame); break;
        case "error": flush(); addNote("error", frame.message); break;
        case "cancelled": flush(); addNote("system", frame.reason || "Stopped by user"); break;
        case "notice":
          if (frame.discarded_chars) text = "";
          flush(); addNote(frame.level === "warning" ? "error" : "system", frame.text); break;
      }
    }
    flush(turn.seq);
  }
  resetPendingTools();
  if (!turns.length) showEmptyState();
  scrollToBottom(true);
}

// --- switching ---------------------------------------------------------------

function switchSession(id, forkEdit = null) {
  if (id === sessionId) return;
  pendingForkEdit = forkEdit;
  sessionId = id;
  history.replaceState(null, "", `#${id}`);
  reconnectNow();
}

export async function forkCurrentSession(throughSeq, regenerateText = undefined) {
  const source = sessionId;
  if (!source || (throughSeq !== null && (!Number.isInteger(throughSeq) || throughSeq < 0))) return false;
  let response;
  try {
    response = await api(`/api/sessions/${encodeURIComponent(source)}/fork`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ through_seq: throughSeq }),
    });
  } catch {
    addNote("error", "Could not fork this conversation.");
    return false;
  }
  if (!response.ok) {
    let message = "Could not fork this conversation.";
    try {
      const data = await response.json();
      if (data.detail) message = data.detail;
    } catch {
      /* use the stable fallback */
    }
    addNote("error", message);
    return false;
  }
  let forked;
  try {
    forked = await response.json();
  } catch {
    addNote("error", "The fork response was not valid JSON.");
    return false;
  }
  if (!forked.id) {
    addNote("error", "The fork response did not include a session id.");
    return false;
  }
  // If the user navigated elsewhere while the request was in flight, keep the
  // successfully-created fork in the sidebar but do not steal their new view.
  if (sessionId !== source) {
    refreshSessions();
    return true;
  }
  const forkEdit =
    regenerateText === undefined
      ? null
      : { session: forked.id, seq: throughSeq, text: regenerateText };
  switchSession(forked.id, forkEdit);
  return true;
}

function fillModels(data) {
  const select = $("new-model");
  const selected = select.value; select.textContent = "";
  for (const [backend, label] of [["claude", "Claude Code"], ["codex", "Codex"], ["lingcore", "LingCore · cost-effective"]]) {
    const group = document.createElement("optgroup"); group.label = label;
    for (const model of data.models.filter((m) => m.backend === backend)) {
      const option = document.createElement("option"); option.value = model.id; option.textContent = model.label;
      group.append(option);
    }
    select.append(group);
  }
  if (selected && data.models.some((m) => m.id === selected)) select.value = selected;
  if (!$("new-workspace").value) $("new-workspace").value = data.recent_workspaces?.[0] || data.workspace || "";
  $("recent-workspaces").replaceChildren(...(data.recent_workspaces || []).map((path) => {
    const option = document.createElement("option"); option.value = path; return option;
  }));
}

export async function newChat() {
  closeSidebar();
  const dialog = $("new-chat-dialog");
  if (!dialog.open) dialog.showModal();
  $("new-chat-error").textContent = "";
  try {
    const response = await api("/api/models?live=false");
    if (!response.ok) throw new Error("Open the token URL printed by agentgui to authenticate.");
    fillModels(await response.json());
    api("/api/models").then((res) => res.json()).then((data) => {
      if (dialog.open && data.models) fillModels(data);
    }).catch(() => {});
  } catch (error) { $("new-chat-error").textContent = error.message; }
}

$("new-chat-cancel").addEventListener("click", () => $("new-chat-dialog").close());
$("new-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("new-chat-create"); button.disabled = true;
  try {
    const response = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: $("new-model").value, workspace: $("new-workspace").value, autonomy: $("new-autonomy").value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the model and workspace directory.");
    $("new-chat-dialog").close();
    switchSession(data.id);
  } catch (error) { $("new-chat-error").textContent = error.message; }
  finally { button.disabled = false; }
});

async function deleteSession(id) {
  let res;
  try {
    res = await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch {
    return;
  }
  if (res.status === 409) {
    addNote("error", "Close this chat first — it is the open session (use New chat).");
    return;
  }
  refreshSessions();
}

// --- mobile drawer -------------------------------------------------------------

function openSidebar() {
  document.body.classList.add("sidebar-open");
  backdrop.hidden = false;
}

function closeSidebar() {
  document.body.classList.remove("sidebar-open");
  backdrop.hidden = true;
}

menuBtn.addEventListener("click", openSidebar);
sidebarClose.addEventListener("click", closeSidebar);
backdrop.addEventListener("click", closeSidebar);
