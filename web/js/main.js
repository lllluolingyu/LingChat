// LingChat browser client — composition root.
//
// Wires the modules together and owns what's left: the protocol dispatch
// (handle() routes lingchat/server.py:_event_to_msg messages to the thread
// view), the composer with its attachment tray, the theme toggle, and boot.
// Plain ES modules, no build step: the browser loads this file directly.

import {
  connect,
  initConnection,
  showConfirm,
  socketOpen,
  suspendReconnect,
  wsSend,
} from "./connection.js";
import {
  adoptSession,
  getSessionId,
  loadThread,
  newChat,
  refreshSessions,
  setChatTitle,
} from "./sessions.js";
import {
  abandonStreaming,
  addNote,
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
  showTyping,
  stickToBottom,
  addAgentMarkdown,
} from "./thread.js";

const $ = (id) => document.getElementById(id);

const formEl = $("composer");
const inputEl = $("input");
const sendEl = $("send");
const attachEl = $("attach");
const fileInputEl = $("file-input");
const attachmentTrayEl = $("attachment-tray");
const agentChip = $("agent-chip");
const newChatEl = $("new-chat");
const themeToggle = $("theme-toggle");
const themeLabel = $("theme-label");

// Focusing the input pops the on-screen keyboard on touch devices — only
// auto-focus where a hardware pointer/keyboard is the norm.
const autoFocus = matchMedia("(hover: hover)").matches;
const focusInput = () => {
  if (autoFocus) inputEl.focus();
};

let connected = false;
let pendingAttachments = [];

const MAX_ATTACHMENTS = 4;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
const ALLOWED_MEDIA = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
]);

// --- event stream -----------------------------------------------------------

function handle(msg) {
  switch (msg.type) {
    case "hello": {
      const agentName = msg.agent || "the agent";
      setAgentIdentity(agentName, msg.model || "");
      agentChip.textContent = `${msg.agent} · ${msg.model}`;
      agentChip.title = `Workspace: ${msg.workspace}`;
      agentChip.hidden = false;
      inputEl.placeholder = `Message ${agentName}…`;
      adoptSession(msg.session || null); // server is authoritative
      clearThread();
      setChatTitle(msg.title || "");
      loadThread();
      break;
    }
    case "session_busy":
      suspendReconnect();
      addNote(
        "error",
        "This session is open in another tab — close it there, or pick another session.",
      );
      break;
    case "text":
      appendAgentText(msg.text);
      break;
    case "tool_call":
      finalizeAgentMessage();
      hideTyping();
      addToolCard(msg.id, msg.name, msg.arguments);
      break;
    case "tool_result":
      resolveToolCard(msg.id, msg.name, msg.ok, msg.content, msg.attachments || []);
      showTyping(); // the model is reading the result
      break;
    case "skill":
      addNote("skill", `Skill ${msg.active ? "activated" : "deactivated"}: ${msg.name}`);
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
      addNote("retry", `${msg.reason}; retrying (${msg.attempt}/${msg.max_attempts})`);
      showTyping();
      break;
    case "confirm":
      hideTyping();
      showConfirm(msg.command, msg.id);
      break;
    case "final":
      // Streamed text already rendered; ensure a bubble exists if Final
      // arrived without prior deltas.
      if (isStreaming()) finalizeAgentMessage(msg.text || undefined);
      else if (msg.text) addAgentMarkdown(msg.text);
      hideTyping();
      break;
    case "turn_end":
      finalizeAgentMessage();
      hideTyping();
      refreshSessions(); // titles / counts / ordering may have changed
      break;
    case "error":
      finalizeAgentMessage();
      hideTyping();
      addNote("error", msg.message);
      break;
  }
}

// --- composer ----------------------------------------------------------------

function updateSendState() {
  sendEl.disabled = !connected || (!inputEl.value.trim() && !pendingAttachments.length);
}

function mediaTypeForFile(file) {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".gif")) return "image/gif";
  if (name.endsWith(".webp")) return "image/webp";
  if (name.endsWith(".pdf")) return "application/pdf";
  return "";
}

async function fileToAttachment(file) {
  const mediaType = mediaTypeForFile(file);
  if (!ALLOWED_MEDIA.has(mediaType)) throw new Error(`unsupported file type: ${mediaType || file.name}`);
  const limit = mediaType.startsWith("image/") ? MAX_IMAGE_BYTES : MAX_FILE_BYTES;
  if (file.size > limit) throw new Error(`${file.name} is too large (${file.size} bytes)`);
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error || new Error("failed to read file"));
    reader.readAsDataURL(file);
  });
  const data = dataUrl.split(",", 2)[1] || "";
  return {
    kind: mediaType.startsWith("image/") ? "image" : "file",
    media_type: mediaType,
    data,
    name: file.name || (mediaType === "application/pdf" ? "attachment.pdf" : "image.png"),
  };
}

function pendingTotalBytes() {
  // Decoded size from base64 length (4 chars -> 3 bytes), close enough for the cap.
  return pendingAttachments.reduce((n, a) => n + Math.floor(a.data.length * 0.75), 0);
}

async function addFiles(files) {
  for (const file of files) {
    if (pendingAttachments.length >= MAX_ATTACHMENTS) {
      addNote("error", `You can attach at most ${MAX_ATTACHMENTS} files per message.`);
      break;
    }
    if (pendingTotalBytes() + file.size > MAX_TOTAL_BYTES) {
      addNote("error", `Attachments exceed the ${Math.floor(MAX_TOTAL_BYTES / (1024 * 1024))}MB total limit per message.`);
      break;
    }
    try {
      pendingAttachments.push(await fileToAttachment(file));
    } catch (e) {
      addNote("error", e.message || String(e));
    }
  }
  renderAttachmentTray();
  updateSendState();
}

function renderAttachmentTray() {
  attachmentTrayEl.textContent = "";
  attachmentTrayEl.hidden = !pendingAttachments.length;
  pendingAttachments.forEach((attachment, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "attachment-chip";
    chip.title = "Remove attachment";
    chip.textContent = `${attachment.kind === "image" ? "🖼" : "📄"} ${attachmentLabel(attachment)} ×`;
    chip.addEventListener("click", () => {
      pendingAttachments.splice(index, 1);
      renderAttachmentTray();
      updateSendState();
    });
    attachmentTrayEl.appendChild(chip);
  });
}

function send() {
  const text = inputEl.value.trim();
  if ((!text && !pendingAttachments.length) || !socketOpen()) return;
  const attachments = pendingAttachments;
  addUserMessage(text, attachments);
  finalizeAgentMessage();
  stickToBottom();
  wsSend({ type: "user", text, attachments });
  inputEl.value = "";
  pendingAttachments = [];
  renderAttachmentTray();
  autosize();
  updateSendState();
  showTyping();
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

inputEl.addEventListener("keydown", (e) => {
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
  themeLabel.textContent = light ? "Light theme" : "Dark theme";
}

themeToggle.addEventListener("click", () => {
  const toLight = document.documentElement.dataset.theme !== "light";
  if (toLight) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  try {
    localStorage.setItem("lingchat-theme", toLight ? "light" : "dark");
  } catch { /* private mode — theme just won't persist */ }
  syncThemeLabel();
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
    updateSendState();
  },
  focusInput,
});

syncThemeLabel();
connect();
refreshSessions();
focusInput();
