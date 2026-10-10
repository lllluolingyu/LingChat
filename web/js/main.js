// LingChat browser client — composition root.
//
// Wires the modules together and owns what's left: the protocol dispatch
// (handle() routes lingchat/server.py:_event_to_msg messages to the thread
// view), the composer with its attachment tray, the theme toggle, and boot.
// Plain ES modules, no build step: the browser loads this file directly.

import {
  api,
  connect,
  initConnection,
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
  isRedundantPluginNote,
  isStreaming,
  resolveToolCard,
  setAgentIdentity,
  setAttachmentDownloadHandler,
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
// Notes re-added after the post-turn transcript reload: history replays
// neither stop/error notes nor transient plugin notices.
let pendingTurnNotes = [];
let pendingAttachments = [];
let forkActive = false;

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

function reloadTranscript(notes = []) {
  const generation = ++transcriptSyncGeneration;
  transcriptSyncing = true;
  setTurnActive(false);
  return loadThread()
    .then(() => {
      if (generation !== transcriptSyncGeneration) return;
      for (const note of [notes].flat()) {
        if (note.plugin) addPluginNote(note.plugin);
        else addNote(note.kind, note.text);
      }
    })
    .finally(() => {
      if (generation !== transcriptSyncGeneration) return;
      transcriptSyncing = false;
      attachEl.disabled = !connected;
      updateSendState();
    });
}


// --- slash-command menu ---------------------------------------------------------
// A listbox that floats above the composer while the input is a bare "/name"
// prefix. Suggestions come from the active server runtime and expansion stays
// server-side: choosing one only inserts its name. The input keeps focus
// throughout (aria-activedescendant), so typing, ↑/↓, Tab/Enter and Esc all act
// on the same field.
let commandMetadata = [];
let commandMatches = [];
let commandActive = 0;
let commandDismissed = false; // Esc closes the menu until the input changes
const commandMenu = document.createElement("div");
commandMenu.id = "command-menu";
commandMenu.className = "command-menu";
commandMenu.setAttribute("role", "listbox");
commandMenu.hidden = true;
formEl.querySelector(".composer-inner").prepend(commandMenu);
inputEl.setAttribute("aria-controls", commandMenu.id);
inputEl.setAttribute("aria-autocomplete", "list");
inputEl.setAttribute("aria-expanded", "false");

function updateCommandSuggestions() {
  const raw = inputEl.value;
  const query = raw.toLowerCase();
  commandMatches = raw.startsWith("/") && !/\s/.test(raw) && !commandDismissed
    ? commandMetadata
        .filter(command => command.name.toLowerCase().startsWith(query))
        .slice(0, 12)
    : [];
  commandActive = 0;
  renderCommandMenu();
}

function renderCommandMenu() {
  const open = commandMatches.length > 0;
  commandMenu.hidden = !open;
  commandMenu.replaceChildren();
  commandMenu.setAttribute("aria-label", "Commands");
  inputEl.setAttribute("aria-expanded", String(open));
  if (!open) {
    inputEl.removeAttribute("aria-activedescendant");
    return;
  }
  const head = document.createElement("div");
  head.className = "command-menu-head";
  head.setAttribute("aria-hidden", "true");
  const title = document.createElement("span");
  title.textContent = "Commands";
  const keys = document.createElement("span");
  keys.className = "command-menu-keys";
  keys.textContent = "↑↓ to choose · Tab to insert · Esc to close";
  head.append(title, keys);
  commandMenu.appendChild(head);
  commandMatches.forEach((command, index) => {
    const option = document.createElement("div");
    option.id = `command-option-${index}`;
    option.className = "command-option";
    option.setAttribute("role", "option");
    const line = document.createElement("span");
    line.className = "command-line";
    const name = document.createElement("span");
    name.className = "command-name";
    name.textContent = command.name;
    line.appendChild(name);
    if (command.argument_hint) {
      const hint = document.createElement("span");
      hint.className = "command-hint";
      hint.textContent = command.argument_hint;
      line.appendChild(hint);
    }
    option.appendChild(line);
    if (command.description) {
      const description = document.createElement("span");
      description.className = "command-desc";
      description.textContent = command.description;
      option.appendChild(description);
    }
    // mousedown would move focus off the input and close the keyboard on phones.
    option.addEventListener("mousedown", event => event.preventDefault());
    option.addEventListener("mousemove", () => setCommandActive(index));
    option.addEventListener("click", () => acceptCommand(index));
    commandMenu.appendChild(option);
  });
  setCommandActive(commandActive);
}

function setCommandActive(index) {
  commandActive = index;
  for (const option of commandMenu.querySelectorAll(".command-option")) {
    const selected = option.id === `command-option-${index}`;
    option.setAttribute("aria-selected", String(selected));
    if (selected) option.scrollIntoView({ block: "nearest" });
  }
  inputEl.setAttribute("aria-activedescendant", `command-option-${index}`);
}

function acceptCommand(index = commandActive) {
  const command = commandMatches[index];
  if (!command) return;
  inputEl.value = command.name + " ";
  inputEl.focus();
  inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
  updateCommandSuggestions(); // the trailing space closes the menu
  autosize();
  updateSendState();
}

inputEl.addEventListener("input", () => {
  commandDismissed = false;
  updateCommandSuggestions();
});
inputEl.addEventListener("blur", () => {
  commandMatches = [];
  renderCommandMenu();
});
inputEl.addEventListener("focus", updateCommandSuggestions);
// Capture phase, so an open menu takes Enter before the composer's send handler.
inputEl.addEventListener("keydown", event => {
  if (commandMenu.hidden || event.isComposing) return;
  const last = commandMatches.length - 1;
  switch (event.key) {
    case "ArrowDown": setCommandActive(commandActive >= last ? 0 : commandActive + 1); break;
    case "ArrowUp": setCommandActive(commandActive <= 0 ? last : commandActive - 1); break;
    case "Tab": acceptCommand(); break;
    case "Enter":
      if (event.shiftKey) return;
      acceptCommand();
      break;
    case "Escape":
      commandDismissed = true;
      updateCommandSuggestions();
      break;
    default: return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);

function handle(msg) {
  switch (msg.type) {
    case "hello": {
      updateLimits(msg.limits);
      commandMetadata = Array.isArray(msg.commands) ? msg.commands : [];
      updateCommandSuggestions();
      const agentName = msg.agent || "the agent";
      setAgentIdentity(agentName, msg.model || "");
      agentChip.textContent = `${msg.agent} · ${msg.model}`;
      agentChip.title = `Workspace: ${msg.workspace}`;
      agentChip.hidden = false;
      inputEl.placeholder = `Message ${agentName}…`;
      adoptSession(msg.session || null); // server is authoritative
      clearThread();
      setChatTitle(msg.title || "");
      pendingTurnNotes = [];
      reloadTranscript().then(() => {
        const pending = takePendingForkEdit(msg.session);
        if (!pending) return;
        if (!wsSend({ type: "edit", seq: pending.seq, text: pending.text })) {
          addNote("error", "The fork was created, but regeneration could not start.");
          return;
        }
        pendingTurnNotes = [];
        setTurnActive(true);
        showTyping();
      });
      break;
    }
    case "turn_busy":
      addNote("error", "A turn is already running. Stop it before sending another message.");
      break;
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
    case "plugin_notice":
      addPluginNote(msg);
      if (!isRedundantPluginNote(msg)) pendingTurnNotes.push({ plugin: msg });
      break;
    case "skill":
      addNote("skill", `Skill ${msg.active ? "activated" : "deactivated"}: ${msg.name}`);
      break;
    case "todos": {
      const { done, total } = todoCounts(msg.todos);
      addTodoCard(msg.todos, total ? `Todos ${done}/${total}` : "Todo list cleared");
      break;
    }
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
      showConfirm(msg.command, msg.id, msg.allowlist_pattern, msg.runner, msg.kind);
      break;
    case "shell_allowlist":
      if (msg.added && msg.pattern) {
        addNote("system", `Allowed ${msg.pattern} for the rest of this session.`);
      } else if (msg.pattern) {
        addNote("system", `${msg.pattern} was already allowed for this session.`);
      } else {
        addNote("system", "That command can't be added to a session allowlist.");
      }
      break;
    case "cancelled":
      removeStreamingMessage();
      cancelPendingTools();
      resetConfirms();
      hideTyping();
      {
        const note = { kind: "system", text: msg.reason || "Stopped by user" };
        pendingTurnNotes.push(note);
        addNote(note.kind, note.text);
      }
      break;
    case "edit_accepted":
      showTyping();
      break;
    case "edit_rejected":
      hideTyping();
      reloadTranscript({
        kind: "error",
        text: msg.message || "The message could not be edited.",
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
      if (isStreaming()) finalizeAgentMessage(msg.text || undefined);
      else if (msg.text) addAgentMarkdown(msg.text);
      hideTyping();
      break;
    case "turn_end":
      finalizeAgentMessage();
      hideTyping();
      setTurnActive(false);
      {
        const notes = pendingTurnNotes;
        pendingTurnNotes = [];
        reloadTranscript(notes);
      }
      break;
    case "error":
      finalizeAgentMessage();
      hideTyping();
      addNote("error", msg.message);
      pendingTurnNotes.push({ kind: "error", text: msg.message });
      break;
  }
}

// --- composer ----------------------------------------------------------------

function updateSendState() {
  sendEl.disabled =
    turnActive ||
    transcriptSyncing ||
    !connected ||
    (!inputEl.value.trim() && !pendingAttachments.length);
  stopEl.disabled = !turnActive || !connected;
}

function mediaTypeForFile(file) {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  const extensionTypes = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".xml": "application/xml",
    ".js": "application/javascript",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".sh": "application/x-sh",
  };
  for (const [extension, mediaType] of Object.entries(extensionTypes)) {
    if (name.endsWith(extension)) return mediaType;
  }
  return "";
}

const TEXTUAL_APPLICATION_TYPES = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "application/ecmascript",
  "application/yaml",
  "application/x-yaml",
  "application/toml",
  "application/x-sh",
  "application/x-shellscript",
]);

// Cosmetic only: LingCore inspects the decoded bytes and may reclassify this.
function displayKind(mediaType) {
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType === "application/pdf") return "file";
  if (mediaType.startsWith("text/") || TEXTUAL_APPLICATION_TYPES.has(mediaType)) {
    return "text";
  }
  return "binary";
}

async function fileToAttachment(file) {
  const mediaType = mediaTypeForFile(file);
  const limit = mediaType.startsWith("image/")
    ? limits.image_max_bytes
    : limits.file_max_bytes;
  if (file.size > limit) throw new Error(`${file.name} is too large (${file.size} bytes)`);
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error || new Error("failed to read file"));
    reader.readAsDataURL(file);
  });
  const data = dataUrl.split(",", 2)[1] || "";
  return {
    kind: displayKind(mediaType),
    media_type: mediaType,
    data,
    name: file.name || "attachment",
  };
}

function pendingTotalBytes() {
  return pendingAttachments.reduce((total, attachment) => {
    const data = attachment.data || "";
    const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
    return total + Math.floor((data.length * 3) / 4) - padding;
  }, 0);
}

async function addFiles(files) {
  for (const file of files) {
    if (pendingAttachments.length >= limits.max_attachments) {
      addNote("error", `You can attach at most ${limits.max_attachments} files per message.`);
      break;
    }
    if (pendingTotalBytes() + file.size > limits.total_max_bytes) {
      addNote("error", `Attachments exceed the ${Math.floor(limits.total_max_bytes / (1024 * 1024))}MB total limit per message.`);
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
    chip.title = `${attachment.media_type || "unknown media type"} · Remove attachment`;
    const icons = { image: "🖼", file: "📄", text: "📝", binary: "📦" };
    chip.textContent = `${icons[attachment.kind] || "📦"} ${attachmentLabel(attachment)} ×`;
    chip.addEventListener("click", () => {
      pendingAttachments.splice(index, 1);
      renderAttachmentTray();
      updateSendState();
    });
    attachmentTrayEl.appendChild(chip);
  });
}

function send() {
  const text = inputEl.value;
  if (
    turnActive ||
    transcriptSyncing ||
    (!text.trim() && !pendingAttachments.length) ||
    !socketOpen()
  ) return;
  const attachments = pendingAttachments;
  if (!wsSend({ type: "user", text, attachments })) return;
  pendingTurnNotes = [];
  addUserMessage(text, attachments);
  finalizeAgentMessage();
  stickToBottom();
  inputEl.value = "";
  updateCommandSuggestions();
  pendingAttachments = [];
  renderAttachmentTray();
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
  if (turnActive || transcriptSyncing || !socketOpen()) return false;
  if (!wsSend({ type: "edit", seq, text })) return false;
  pendingTurnNotes = [];
  setTurnActive(true);
  return true;
});

setForkHandler(async (seq, regenerateText = undefined) => {
  if (turnActive || transcriptSyncing || forkActive || !socketOpen()) return false;
  forkActive = true;
  try {
    const forked = await forkCurrentSession(seq, regenerateText);
    if (forked) pendingTurnNotes = [];
    return forked;
  } finally {
    forkActive = false;
  }
});

setAttachmentDownloadHandler(async (seq, index, name) => {
  const session = getSessionId();
  if (!session) throw new Error("No stored session is active");
  const response = await api(
    `/api/sessions/${encodeURIComponent(session)}/messages/${seq}/attachments/${index}`,
  );
  if (!response.ok) throw new Error("Attachment download failed");
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
    stopEl.disabled = !c || !turnActive;
    attachEl.disabled = !c || turnActive || transcriptSyncing;
    updateSendState();
  },
  focusInput,
});

syncThemeLabel();
connect();
refreshSessions();
focusInput();
