/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// AgentGUI browser client — composition root.
//
// Wires the modules together and owns what's left: the protocol dispatch
// (handle() routes agentgui protocol frames to the thread
// view), the composer with its attachment tray, the theme toggle, and boot.
// Plain ES modules, no build step: the browser loads this file directly.

import { createAttachmentQueue } from "/shared/attachments.js";
import { createCommandMenu } from "/shared/command-menu.js";
import {
  LANG_NAMES,
  apply as applyI18n,
  nextLang,
  onLangChange,
  setLang,
  t,
} from "./i18n.js";
import {
  api,
  connect,
  initConnection,
  refreshStatusLabel,
  resetConfirms,
  showConfirm,
  socketOpen,
  suspendReconnect,
  wsSend,
} from "./connection.js";
import {
  adoptSession,
  forkCurrentSession,
  getSessionId,
  loadThread,
  newChat,
  refreshChatTitle,
  refreshSessions,
  setChatTitle,
  takePendingForkEdit,
} from "./sessions.js";
import {
  abandonStreaming,
  addNote,
  addPluginNote,
  addToolCard,
  addUserMessage,
  appendAgentText,
  attachmentLabel,
  clearThread,
  compactNote,
  discardAgentMessage,
  finalizeAgentMessage,
  hideTyping,
  isStreaming,
  resolveToolCard,
  setAgentIdentity,
  setAttachmentDownloadHandler,
  setCapabilities,
  appendThinking,
  renderUsage,
  showTyping,
  stickToBottom,
  addAgentMarkdown,
  cancelPendingTools,
  removeStreamingMessage,
  setEditHandler,
  setForkHandler,
  addTodoCard,
  todoCounts,
} from "./thread.js";

function todoTitle(todos) {
  const { done, total } = todoCounts(todos);
  return total ? t("todo.title", { done, total }) : t("todo.cleared");
}

const $ = (id) => document.getElementById(id);

const formEl = $("composer");
const inputEl = $("input");
const sendEl = $("send");
const stopEl = $("stop");
const attachEl = $("attach");
const fileInputEl = $("file-input");
const attachmentTrayEl = $("attachment-tray");
const agentChip = $("agent-chip");
const newChatEl = $("new-chat");
const themeToggle = $("theme-toggle");
const themeLabel = $("theme-label");
const langToggle = $("lang-toggle");
const langLabel = $("lang-label");

// Focusing the input pops the on-screen keyboard on touch devices — only
// auto-focus where a hardware pointer/keyboard is the norm.
const autoFocus = matchMedia("(hover: hover)").matches;
const focusInput = () => {
  if (autoFocus) inputEl.focus();
};

let connected = false;
let turnActive = false;
let transcriptSyncing = false;
let transcriptSyncGeneration = 0;
let forkActive = false;
let currentCapabilities = {};
// The last "hello", kept so a language change can relabel the chrome it set
// without waiting for a reconnect.
let identity = null;

let limits = {
  max_attachments: 8,
  image_max_bytes: 5 * 1024 * 1024,
  file_max_bytes: 10 * 1024 * 1024,
  total_max_bytes: 20 * 1024 * 1024,
};

function updateLimits(incoming) {
  if (!incoming || typeof incoming !== "object") return;
  for (const key of Object.keys(limits)) {
    const value = Number(incoming[key]);
    if (Number.isSafeInteger(value) && value > 0) limits[key] = value;
  }
}

// --- event stream -----------------------------------------------------------

function setTurnActive(active) {
  turnActive = active;
  document.body.classList.toggle("turn-active", active);
  sendEl.hidden = active;
  stopEl.hidden = !active;
  stopEl.disabled = !connected;
  attachEl.disabled = active || transcriptSyncing || !connected;
  updateSendState();
}

function reloadTranscript(note = null) {
  const generation = ++transcriptSyncGeneration;
  transcriptSyncing = true;
  setTurnActive(false);
  return loadThread()
    .then(() => {
      if (generation === transcriptSyncGeneration && note) {
        addNote(note.kind, note.text);
      }
    })
    .finally(() => {
      if (generation !== transcriptSyncGeneration) return;
      transcriptSyncing = false;
      attachEl.disabled = !connected;
      updateSendState();
    });
}

// --- slash-command menu -----------------------------------------------------
const commands = createCommandMenu({
  inputEl,
  formEl,
  onChange() {
    autosize();
    updateSendState();
  },
  t,
});

function handle(msg) {
  switch (msg.type) {
    case "hello": {
      updateLimits(msg.limits);
      commands.setCommands(msg.commands);
      identity = msg;
      const agentName = msg.agent || msg.backend || t("thread.the_agent");
      setAgentIdentity(agentName, msg.model || "", msg.backend || "");
      currentCapabilities = msg.capabilities || {};
      setCapabilities(currentCapabilities);
      $("fork-chat").hidden = !currentCapabilities.fork;
      $("usage-chip").hidden = true;
      fileInputEl.accept = currentCapabilities.files ? "" : "image/png,image/jpeg,image/gif,image/webp";
      attachEl.hidden = !currentCapabilities.images && !currentCapabilities.files;
      agentChip.textContent = `${msg.model} · ${msg.autonomy}`;
      agentChip.title = t("thread.workspace", { path: msg.workspace });
      agentChip.hidden = false;
      inputEl.placeholder = t("composer.placeholder_named", { name: agentName });
      adoptSession(msg.session || null); // server is authoritative
      attachments.reset();
      clearThread();
      setChatTitle(msg.title || "");

      reloadTranscript().then(() => {
        const pending = takePendingForkEdit(msg.session);
        if (!pending) return;
        if (!wsSend({ type: "edit", seq: pending.seq, text: pending.text })) {
          addNote("error", t("note.fork_regen_failed"));
          return;
        }

        setTurnActive(true);
        showTyping();
      });
      break;
    }
    case "turn_busy":
      addNote("error", t("note.turn_busy"));
      break;
    case "session_busy":
      suspendReconnect();
      addNote("error", t("note.session_busy"));
      break;
    case "text":
      appendAgentText(msg.delta ?? msg.text ?? "");
      break;
    case "thinking":
      appendThinking(msg.delta || "");
      break;
    case "tool_call":
      finalizeAgentMessage();
      hideTyping();
      addToolCard(msg.id, msg.name, msg.arguments);
      break;
    case "tool_result":
      resolveToolCard(msg.id, msg.name, msg.ok, msg.content, msg.attachments || [], msg.diff);
      showTyping(); // the model is reading the result
      break;
    case "plugin_notice":
      addPluginNote(msg);
      break;
    case "skill":
      addNote("skill", t(msg.active ? "note.skill_on" : "note.skill_off", { name: msg.name }));
      break;
    case "todos":
      addTodoCard(msg.todos, todoTitle(msg.todos));
      break;
    case "compact":
      finalizeAgentMessage();
      addNote("compact", compactNote(msg));
      showTyping();
      break;
    case "stream_retry":
      // The in-flight reply was lost mid-stream; whatever the current
      // bubble holds is void and will be regenerated (possibly differently).
      if (isStreaming() && msg.discarded_chars) discardAgentMessage();
      else abandonStreaming();
      addNote(
        "retry",
        t("note.retry", {
          reason: msg.reason,
          attempt: msg.attempt,
          max: msg.max_attempts,
        }),
      );
      showTyping();
      break;
    case "approval":
      hideTyping();
      showConfirm(msg);
      break;
    case "cancelled":
      removeStreamingMessage();
      cancelPendingTools();
      resetConfirms();
      hideTyping();
      addNote("system", msg.reason || t("note.stopped"));
      break;
    case "edit_accepted":
      showTyping();
      break;
    case "edit_rejected":
      hideTyping();
      reloadTranscript({
        kind: "error",
        text: msg.message || t("note.edit_failed"),
      });
      break;
    case "stop_ignored":
      resetConfirms();
      hideTyping();
      reloadTranscript();
      break;
    case "final":
      // Streamed text already rendered; ensure a bubble exists if Final
      // arrived without prior deltas.
      if (isStreaming()) finalizeAgentMessage(msg.content ?? msg.text ?? undefined);
      else if (msg.content || msg.text) addAgentMarkdown(msg.content ?? msg.text);
      hideTyping();
      break;
    case "turn_end":
      finalizeAgentMessage();
      hideTyping();
      setTurnActive(false);
      resetConfirms();
      reloadTranscript();
      break;
    case "error":
      finalizeAgentMessage();
      hideTyping();
      addNote("error", msg.message);
      break;
    case "notice":
      if (msg.discarded_chars && isStreaming()) discardAgentMessage();
      addNote(msg.level === "warning" ? "error" : "system", msg.text || "");
      break;
    case "usage":
      renderUsage(msg);
      break;
  }
}

// --- composer ----------------------------------------------------------------

function updateSendState() {
  sendEl.disabled =
    turnActive ||
    transcriptSyncing ||
    attachments.busy ||
    !connected ||
    (!inputEl.value.trim() && !attachments.items.length);
  stopEl.disabled = !turnActive || !connected;
}

const attachments = createAttachmentQueue({
  getLimits: () => limits,
  getCapabilities: () => currentCapabilities,
  t,
  onChange() {
    renderAttachmentTray();
    updateSendState();
  },
  onError: (message) => addNote("error", message),
});

function addFiles(files) {
  if (turnActive || transcriptSyncing || !connected) return;
  return attachments.add(files);
}

function renderAttachmentTray() {
  attachmentTrayEl.textContent = "";
  attachmentTrayEl.hidden = !attachments.items.length;
  attachments.items.forEach((attachment, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "attachment-chip";
    chip.title = t("composer.remove_attachment", { type: attachment.media_type || t("composer.unknown_media_type") });
    const icons = { image: "🖼", file: "📄", text: "📝", binary: "📦" };
    chip.textContent = `${icons[attachment.kind] || "📦"} ${attachmentLabel(attachment)} ×`;
    chip.addEventListener("click", () => {
      attachments.remove(index);
    });
    attachmentTrayEl.appendChild(chip);
  });
}

function send() {
  const text = inputEl.value;
  if (
    turnActive ||
    transcriptSyncing ||
    attachments.busy ||
    (!text.trim() && !attachments.items.length) ||
    !socketOpen()
  ) return;
  const batch = attachments.items;
  if (!wsSend({ type: "user", text, attachments: batch })) return;

  addUserMessage(text, batch);
  finalizeAgentMessage();
  stickToBottom();
  inputEl.value = "";
  commands.update();
  attachments.reset();
  autosize();
  updateSendState();
  setTurnActive(true);
  showTyping();
}

stopEl.addEventListener("click", () => {
  if (!turnActive || !wsSend({ type: "stop" })) return;
  stopEl.disabled = true;
});

setEditHandler((seq, text) => {
  if (turnActive || transcriptSyncing || attachments.busy || !socketOpen()) return false;
  if (!wsSend({ type: "edit", seq, text })) return false;

  setTurnActive(true);
  return true;
});

setForkHandler(async (seq, regenerateText = undefined) => {
  if (turnActive || transcriptSyncing || forkActive || !socketOpen()) return false;
  forkActive = true;
  try {
    return await forkCurrentSession(seq, regenerateText);
  } finally {
    forkActive = false;
  }
});

setAttachmentDownloadHandler(async (seq, index, name) => {
  const session = getSessionId();
  if (!session) throw new Error(t("thread.no_session"));
  const response = await api(
    `/api/sessions/${encodeURIComponent(session)}/messages/${seq}/attachments/${index}`,
  );
  if (!response.ok) throw new Error(t("thread.attachment_download_failed"));
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name || "attachment";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
});

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

inputEl.addEventListener("keydown", (e) => {
  if (commands.handleKeydown(e)) return;
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}
inputEl.addEventListener("input", () => {
  autosize();
  updateSendState();
});

attachEl.addEventListener("click", () => fileInputEl.click());
fileInputEl.addEventListener("change", () => {
  addFiles(fileInputEl.files || []);
  fileInputEl.value = "";
});
inputEl.addEventListener("paste", (e) => {
  const files = Array.from(e.clipboardData?.files || []);
  if (files.length) addFiles(files);
});

// --- theme ---------------------------------------------------------------------

function syncThemeLabel() {
  const light = document.documentElement.dataset.theme === "light";
  themeLabel.textContent = t(light ? "app.theme_light" : "app.theme_dark");
}

themeToggle.addEventListener("click", () => {
  const toLight = document.documentElement.dataset.theme !== "light";
  if (toLight) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  try {
    localStorage.setItem("agentgui-theme", toLight ? "light" : "dark");
  } catch { /* private mode — theme just won't persist */ }
  syncThemeLabel();
});

// --- language ------------------------------------------------------------------

// The button offers the language it would switch to, so its own label never
// needs translating.
function syncLangLabel() {
  langLabel.textContent = LANG_NAMES[nextLang()];
}

langToggle.addEventListener("click", () => setLang(nextLang()));

// A language change repaints everything already on screen. The static markup is
// handled by the data-i18n pass; the rest is chrome this module built itself,
// plus the transcript, whose tool and note labels were rendered in the old
// language and are cheapest to get right by replaying it.
onLangChange(() => {
  applyI18n();
  syncThemeLabel();
  syncLangLabel();
  refreshStatusLabel();
  refreshChatTitle();
  refreshSessions();
  if (identity) {
    const agentName = identity.agent || identity.backend || t("thread.the_agent");
    setAgentIdentity(agentName, identity.model || "", identity.backend || "");
    agentChip.title = t("thread.workspace", { path: identity.workspace });
    inputEl.placeholder = t("composer.placeholder_named", { name: agentName });
  } else {
    inputEl.placeholder = t("composer.placeholder");
  }
  commands.render();
  renderAttachmentTray();
  if (!turnActive && !transcriptSyncing) reloadTranscript();
});

// --- boot -------------------------------------------------------------------------

newChatEl.addEventListener("click", () => {
  newChat();
  focusInput();
});

initConnection({
  getSession: getSessionId,
  onMessage: handle,
  onConnectedChange: (c) => {
    connected = c;
    if (!c) attachments.reset();
    if (!c) { setTurnActive(false); resetConfirms(); }
    stopEl.disabled = !c || !turnActive;
    attachEl.disabled = !c || turnActive || transcriptSyncing;
    updateSendState();
  },
  focusInput,
});

applyI18n();
syncThemeLabel();
syncLangLabel();
if (getSessionId()) connect();
else newChat();
refreshSessions();
focusInput();

$("fork-chat").addEventListener("click", async () => {
  if (turnActive || forkActive || transcriptSyncing) return;
  forkActive = true;
  try { await forkCurrentSession(null); } finally { forkActive = false; }
});
$("doctor-open").addEventListener("click", async () => {
  const dialog = $("doctor-dialog"); dialog.showModal();
  const results = $("doctor-results"); results.textContent = t("app.doctor_running");
  try {
    const response = await api("/api/doctor");
    if (!response.ok) throw new Error(t("app.auth_hint"));
    const data = await response.json(); results.textContent = "";
    for (const check of data.checks) {
      const item = document.createElement("p");
      item.textContent = `${check.ok ? "✓" : "⚠"} ${check.name}: ${check.detail}`;
      results.append(item);
    }
  } catch (error) { results.textContent = error.message; }
});
$("doctor-close").addEventListener("click", () => $("doctor-dialog").close());
