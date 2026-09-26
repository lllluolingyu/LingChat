/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// Thread view: renders messages, tool cards, notes, the typing indicator and
// the streaming assistant reply into #thread, and owns the state those need
// (in-flight stream, unresolved tool cards, scroll stickiness).
//
// Tool cards are keyed by the LingCore tool-call id (ToolCall.id /
// ToolResult.call_id, carried through the wire protocol), so parallel calls
// to the same tool resolve the right card; name matching is only a fallback
// for an id-less message.

import { renderMarkdown, attachCopyHandler, COPY_ICON_SVG } from "./markdown.js";

const $ = (id) => document.getElementById(id);

const messagesEl = $("messages");
const threadEl = $("thread");
const jumpBtn = $("jump-bottom");

let streaming = null; // {body, raw, rafId, col} for the assistant reply in flight
let pendingTools = []; // [{id, name, status, body}] tool calls awaiting their result
let typingEl = null;
let stick = true; // keep the view glued to the newest message
let agentName = "the agent";
let modelName = "";
let editHandler = null; // (seq, editedText) => boolean, injected by main.js
let forkHandler = null; // async (seq, regenerateText?) => boolean, injected by main.js
let attachmentDownloadHandler = null; // async (seq, index, name), injected by main.js
let capabilities = {};
let thinking = null;

export function setAgentIdentity(name, model) {
  agentName = name || "the agent";
  modelName = model || "";
}

export function setEditHandler(handler) {
  editHandler = handler;
}

export function setForkHandler(handler) {
  forkHandler = handler;
}

export function setAttachmentDownloadHandler(handler) {
  attachmentDownloadHandler = handler;
}

export function setCapabilities(incoming) {
  capabilities = { ...capabilities, ...(incoming || {}) };
}

// --- scrolling ---------------------------------------------------------------

function nearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 90;
}

export function scrollToBottom(force = false) {
  if (!force && !stick) return;
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// Re-glue the view to the newest message (used when the user sends).
export function stickToBottom() {
  stick = true;
  jumpBtn.hidden = true;
  scrollToBottom(true);
}

messagesEl.addEventListener("scroll", () => {
  stick = nearBottom();
  jumpBtn.hidden = stick;
});

jumpBtn.addEventListener("click", () => {
  stick = true;
  jumpBtn.hidden = true;
  messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
});

// --- rows and attachments ------------------------------------------------------

const AVATAR_SVG =
  '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 6.5l1.9 6 6.1 1.9-6.1 1.9-1.9 6-1.9-6-6.1-1.9 6.1-1.9z" fill="white"/></svg>';

function removeEmptyState() {
  const el = threadEl.querySelector(".empty-state");
  if (el) el.remove();
}

export function showEmptyState() {
  if (threadEl.childElementCount > 0) return;
  const el = document.createElement("div");
  el.className = "empty-state";
  const mark = document.createElement("div");
  mark.className = "empty-mark";
  mark.innerHTML = AVATAR_SVG;
  const title = document.createElement("div");
  title.className = "empty-title";
  title.textContent = `Chat with ${agentName}`;
  const sub = document.createElement("div");
  sub.className = "empty-sub";
  sub.textContent = modelName
    ? `Running on ${modelName}. Messages and tool activity will appear here.`
    : "Messages and tool activity will appear here.";
  el.append(mark, title, sub);
  threadEl.appendChild(el);
}

function row(cls) {
  removeEmptyState();
  const el = document.createElement("div");
  el.className = `row ${cls}`;
  threadEl.appendChild(el);
  return el;
}

function attachmentUrl(a) {
  return `data:${a.media_type};base64,${a.data}`;
}

export function attachmentLabel(a) {
  return a.name || a.media_type || "attachment";
}

function humanSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "";
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachments(container, attachments = [], seq = null) {
  if (!attachments.length) return;
  const grid = document.createElement("div");
  grid.className = "attachment-grid";
  attachments.forEach((a, index) => {
    const item = document.createElement("div");
    item.className = "attachment-card";
    if (a.kind === "image") {
      const img = document.createElement("img");
      img.alt = attachmentLabel(a);
      img.src = attachmentUrl(a);
      item.appendChild(img);
    } else {
      const icon = document.createElement("div");
      icon.className = `attachment-file-icon kind-${a.kind || "binary"}`;
      icon.textContent = { file: "PDF", text: "TXT", binary: "BIN" }[a.kind] || "BIN";
      item.appendChild(icon);
    }
    item.title = a.media_type || "";
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = attachmentLabel(a);
    item.appendChild(name);
    const size = humanSize(a.size);
    if (size) {
      const meta = document.createElement("div");
      meta.className = "attachment-size";
      meta.textContent = size;
      item.appendChild(meta);
    }
    if (Number.isInteger(seq) && a.download === true && attachmentDownloadHandler) {
      const download = document.createElement("button");
      download.type = "button";
      download.className = "attachment-download";
      download.textContent = "Download";
      download.addEventListener("click", async () => {
        if (download.disabled) return;
        download.disabled = true;
        try {
          await attachmentDownloadHandler(seq, index, attachmentLabel(a));
        } catch {
          download.textContent = "Failed";
        } finally {
          if (download.isConnected && download.textContent !== "Failed") {
            download.disabled = false;
          }
        }
      });
      item.appendChild(download);
    }
    grid.appendChild(item);
  });
  container.appendChild(grid);
}

function renderUserBubble(bubble, text, attachments, seq = null) {
  bubble.textContent = "";
  bubble.appendChild(document.createTextNode(text || "Attached media"));
  renderAttachments(bubble, attachments, seq);
}

function forkButton(seq, regenerateText = undefined) {
  const fork = document.createElement("button");
  if (!capabilities.fork || !capabilities.fork_at_message) fork.hidden = true;
  fork.type = "button";
  fork.className = "edit-btn fork-btn";
  fork.title =
    regenerateText === undefined
      ? "Fork conversation from here"
      : "Fork and regenerate from this message";
  fork.setAttribute("aria-label", fork.title);
  fork.innerHTML =
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v3.2c0 1.3 1 2.3 2.3 2.3h1.2M4 13.5v-3.2C4 9 5 8 6.3 8h4.2M8.5 5l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Fork</span>';
  fork.addEventListener("click", async () => {
    if (!forkHandler || fork.disabled) return;
    fork.disabled = true;
    let accepted = false;
    try {
      accepted = await forkHandler(seq, regenerateText);
    } catch {
      accepted = false;
    } finally {
      if (!accepted && fork.isConnected) fork.disabled = false;
    }
  });
  return fork;
}

function startUserEdit(r, bubble, actions, text, attachments, seq) {
  if (!editHandler || r.classList.contains("editing")) return;
  r.classList.add("editing");
  actions.hidden = true;
  bubble.textContent = "";

  const input = document.createElement("textarea");
  input.className = "user-edit-input";
  input.value = text;
  input.rows = Math.min(8, Math.max(2, text.split("\n").length));
  input.setAttribute("aria-label", "Edit message");
  bubble.appendChild(input);
  renderAttachments(bubble, attachments, seq);

  const editActions = document.createElement("div");
  editActions.className = "user-edit-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn";
  cancel.textContent = "Cancel";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "btn btn-primary";
  save.textContent = "Save & regenerate";
  editActions.append(cancel, save);
  r.appendChild(editActions);

  const finish = (shouldSave) => {
    if (!r.classList.contains("editing")) return;
    const edited = input.value.trim();
    if (shouldSave && !edited && !attachments.length) return;
    if (shouldSave && !editHandler(seq, edited)) return;
    if (shouldSave) {
      // Editing branches the conversation: remove every rendered row after
      // this user message. The server performs the matching durable rewind.
      while (r.nextElementSibling) r.nextElementSibling.remove();
      text = edited;
    }
    renderUserBubble(bubble, text, attachments, seq);
    editActions.remove();
    actions.hidden = false;
    r.classList.remove("editing");
    scrollToBottom(true);
  };
  cancel.addEventListener("click", () => finish(false));
  save.addEventListener("click", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") finish(false);
    else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) finish(true);
  });
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

export function addUserMessage(text, attachments = [], synthetic = false, seq = null) {
  const r = row(synthetic ? "event" : "user");
  const bubble = document.createElement("div");
  bubble.className = synthetic ? "note system media-note" : "bubble-user";
  renderUserBubble(bubble, text, attachments, seq);
  r.appendChild(bubble);
  if (!synthetic && Number.isInteger(seq)) {
    r.dataset.seq = String(seq);
    const actions = document.createElement("div");
    actions.className = "user-actions";
    const edit = document.createElement("button");
    edit.hidden = !capabilities.edit_regenerate;
    edit.type = "button";
    edit.className = "edit-btn";
    edit.title = "Edit message";
    edit.setAttribute("aria-label", "Edit message");
    edit.innerHTML =
      '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.8 2.3l2.9 2.9-7.5 7.5-3.5.6.6-3.5zM9.7 3.4l2.9 2.9" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Edit</span>';
    edit.addEventListener("click", () =>
      startUserEdit(r, bubble, actions, text, attachments, seq),
    );
    actions.append(edit, forkButton(seq, text));
    r.appendChild(actions);
  }
  scrollToBottom();
  return r;
}

// --- streaming assistant reply ---------------------------------------------------

function agentRow() {
  const r = row("agent");
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.innerHTML = AVATAR_SVG;
  const col = document.createElement("div");
  col.className = "agent-col";
  r.append(avatar, col);
  return col;
}

function startAgentMessage() {
  hideTyping();
  const col = agentRow();
  const body = document.createElement("div");
  body.className = "agent-body md";
  col.appendChild(body);
  streaming = { body, raw: "", rafId: 0, col };
  return streaming;
}

export function isStreaming() {
  return streaming !== null;
}

export function appendAgentText(text) {
  thinking = null;
  if (!streaming) startAgentMessage();
  streaming.raw += text;
  if (!streaming.rafId) {
    streaming.rafId = requestAnimationFrame(() => {
      if (!streaming) return;
      streaming.rafId = 0;
      streaming.body.innerHTML = renderMarkdown(streaming.raw);
      scrollToBottom();
    });
  }
}

// `finalText`, when given, is the authoritative full text from the Final
// event and replaces whatever deltas accumulated.
function appendAgentActions(col, raw, seq = null) {
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-btn";
  copy.title = "Copy message";
  copy.dataset.raw = raw;
  copy.innerHTML = COPY_ICON_SVG + "<span>Copy</span>";
  actions.appendChild(copy);
  if (Number.isInteger(seq)) actions.appendChild(forkButton(seq));
  col.appendChild(actions);
}

export function finalizeAgentMessage(finalText) {
  if (!streaming) return;
  if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
  if (finalText) streaming.raw = finalText;
  streaming.body.innerHTML = renderMarkdown(streaming.raw);
  appendAgentActions(streaming.col, streaming.raw);
  streaming = null;
  scrollToBottom();
}

// The in-flight reply was lost mid-stream: render what arrived, struck
// through, with no copy action — the retry supersedes it.
export function discardAgentMessage() {
  if (!streaming) return;
  if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
  streaming.body.innerHTML = renderMarkdown(streaming.raw);
  streaming.body.classList.add("discarded");
  streaming = null;
}

// Drop the in-flight stream without ceremony (socket died; nothing to keep).
export function abandonStreaming() {
  if (!streaming) return;
  if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
  streaming = null;
}

export function removeStreamingMessage() {
  if (!streaming) return;
  if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
  const r = streaming.col.parentElement;
  streaming = null;
  if (r) r.remove();
}

export function addAgentMarkdown(text, seq = null) {
  hideTyping();
  const col = agentRow();
  const body = document.createElement("div");
  body.className = "agent-body md";
  body.innerHTML = renderMarkdown(text);
  col.appendChild(body);
  appendAgentActions(col, text, seq);
  scrollToBottom();
  return body;
}

// --- tool cards ---------------------------------------------------------------

function short(value, n = 300) {
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length > n ? s.slice(0, n) + " …" : s;
}

// Pick the most human-meaningful argument for the collapsed summary line.
function argsPreview(args) {
  if (args && typeof args === "object") {
    for (const key of ["command", "cmd", "file_path", "path", "pattern", "url", "query", "key", "name"]) {
      if (typeof args[key] === "string" && args[key]) return args[key];
    }
    const s = JSON.stringify(args);
    return s === "{}" ? "" : s;
  }
  return typeof args === "string" ? args : JSON.stringify(args);
}

const OK_ICON =
  '<svg class="ok-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8.4l3 3 6.6-6.8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ERR_ICON =
  '<svg class="err-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const CHEVRON =
  '<svg class="tool-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 6l4.5 4.5L12.5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function toolSection(label, text, isErr = false) {
  const wrap = document.createDocumentFragment();
  const lab = document.createElement("div");
  lab.className = "tool-section-label";
  lab.textContent = label;
  const pre = document.createElement("pre");
  if (isErr) pre.className = "err-text";
  pre.textContent = short(text, 20000);
  wrap.append(lab, pre);
  return wrap;
}

export function addToolCard(id, name, args) {
  thinking = null;
  const r = row("event");
  const card = document.createElement("details");
  card.className = "tool-card";

  const summary = document.createElement("summary");
  const status = document.createElement("span");
  status.className = "tool-status";
  status.innerHTML = '<span class="spinner"></span>';
  const nameEl = document.createElement("span");
  nameEl.className = "tool-name";
  nameEl.textContent = name;
  const preview = document.createElement("span");
  preview.className = "tool-preview";
  preview.textContent = argsPreview(args);
  summary.append(status, nameEl, preview);
  summary.insertAdjacentHTML("beforeend", CHEVRON);

  const body = document.createElement("div");
  body.className = "tool-body";
  const argText =
    args && typeof args === "object" ? JSON.stringify(args, null, 2) : String(args ?? "");
  if (argText && argText !== "{}") body.appendChild(toolSection("Arguments", argText));

  card.append(summary, body);
  r.appendChild(card);
  pendingTools.push({ id: id || null, name, status, body });
  scrollToBottom();
  return card;
}

export function resolveToolCard(id, name, ok, content, attachments = [], diff = null) {
  // Match by call id; a message without one (older wire shape) falls back to
  // the oldest id-less card with the same name.
  const idx = pendingTools.findIndex((t) => (id ? t.id === id : t.name === name));
  let t;
  if (idx >= 0) {
    t = pendingTools.splice(idx, 1)[0];
  } else {
    // A result with no visible call (shouldn't happen, but render honestly).
    addToolCard(id, name, null);
    t = pendingTools.pop();
  }
  t.status.innerHTML = ok ? OK_ICON : ERR_ICON;
  t.body.appendChild(toolSection(ok ? "Result" : "Error", content, !ok));
  if (diff) t.body.appendChild(diffView(diff));
  renderAttachments(t.body, attachments);
  scrollToBottom();
}

// Forget unresolved cards (e.g. a replayed crashed turn): a later result must
// not pair with them.
export function resetPendingTools() {
  pendingTools = [];
}

export function cancelPendingTools() {
  for (const tool of pendingTools) {
    tool.status.innerHTML = ERR_ICON;
    tool.body.appendChild(toolSection("Stopped", "Cancelled by user", true));
  }
  pendingTools = [];
}

// --- notes and typing indicator ---------------------------------------------------

const NOTE_ICONS = { skill: "⚙", retry: "⟲", compact: "⊞", error: "⚠", system: "" };

function tokenEstimate(value) {
  const n = Number(value);
  return Number.isFinite(n) ? `~${Math.max(0, Math.round(n)).toLocaleString()}` : "~?";
}

export function compactNote(msg) {
  const count = Number(msg.summarized_messages);
  const rounded = Number.isFinite(count) ? Math.max(0, Math.round(count)) : null;
  const label =
    rounded === 1
      ? "1 earlier message"
      : `${rounded === null ? "some" : rounded.toLocaleString()} earlier messages`;
  const before = tokenEstimate(msg.before_tokens);
  const after = tokenEstimate(msg.after_tokens);
  return `Context compacted: summarized ${label} (${before} → ${after} tokens).`;
}

export function addNote(kind, text) {
  const r = row("event");
  const note = document.createElement("div");
  note.className = `note ${kind}`;
  const icon = NOTE_ICONS[kind];
  if (icon) {
    const ic = document.createElement("span");
    ic.className = "note-icon";
    ic.textContent = icon;
    note.appendChild(ic);
  }
  note.appendChild(document.createTextNode(text));
  r.appendChild(note);
  scrollToBottom();
}

export function showTyping() {
  if (typingEl) return;
  const r = row("agent");
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.innerHTML = AVATAR_SVG;
  const dots = document.createElement("div");
  dots.className = "typing";
  dots.innerHTML = "<span></span><span></span><span></span>";
  r.append(avatar, dots);
  typingEl = r;
  scrollToBottom();
}

export function hideTyping() {
  if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
}

export function clearThread() {
  thinking = null;
  abandonStreaming();
  threadEl.textContent = "";
  pendingTools = [];
  typingEl = null;
  stick = true;
  jumpBtn.hidden = true;
}

attachCopyHandler(threadEl);

export function appendThinking(delta) {
  hideTyping();
  if (!thinking) {
    const details = document.createElement("details");
    details.className = "thinking-block";
    const summary = document.createElement("summary"); summary.textContent = "Thinking";
    thinking = document.createElement("pre"); details.append(summary, thinking);
    row("event").append(details);
  }
  thinking.append(document.createTextNode(delta));
  scrollToBottom();
}

export function renderUsage(msg) {
  const chip = $("usage-chip");
  const cost = msg.cost_usd != null ? ` · $${Number(msg.cost_usd).toFixed(4)}` : "";
  const context = msg.context_pct != null ? ` · ${Number(msg.context_pct).toFixed(0)}% context` : "";
  chip.textContent = `${Number(msg.input || 0).toLocaleString()} in · ${Number(msg.output || 0).toLocaleString()} out${cost}${context}`;
  chip.title = `${Number(msg.cached || 0).toLocaleString()} cached tokens`;
  chip.hidden = false;
}

function diffView(diff) {
  const pre = document.createElement("pre"); pre.className = "diff-view";
  for (const line of diff.split("\n")) {
    const span = document.createElement("span");
    span.className = line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-remove" : line.startsWith("@@") ? "diff-hunk" : "";
    span.textContent = line + "\n"; pre.append(span);
  }
  return pre;
}
