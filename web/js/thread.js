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

export function setAgentIdentity(name, model) {
  agentName = name || "the agent";
  modelName = model || "";
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

function renderAttachments(container, attachments = []) {
  if (!attachments.length) return;
  const grid = document.createElement("div");
  grid.className = "attachment-grid";
  for (const a of attachments) {
    const item = document.createElement("div");
    item.className = "attachment-card";
    if (a.kind === "image") {
      const img = document.createElement("img");
      img.alt = attachmentLabel(a);
      img.src = attachmentUrl(a);
      item.appendChild(img);
    } else {
      const icon = document.createElement("div");
      icon.className = "attachment-file-icon";
      icon.textContent = "PDF";
      item.appendChild(icon);
    }
    const name = document.createElement("div");
    name.className = "attachment-name";
    name.textContent = attachmentLabel(a);
    item.appendChild(name);
    grid.appendChild(item);
  }
  container.appendChild(grid);
}

export function addUserMessage(text, attachments = [], synthetic = false) {
  const r = row(synthetic ? "event" : "user");
  const bubble = document.createElement("div");
  bubble.className = synthetic ? "note system media-note" : "bubble-user";
  if (text) bubble.appendChild(document.createTextNode(text));
  else bubble.appendChild(document.createTextNode("Attached media"));
  renderAttachments(bubble, attachments);
  r.appendChild(bubble);
  scrollToBottom();
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
export function finalizeAgentMessage(finalText) {
  if (!streaming) return;
  if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
  if (finalText) streaming.raw = finalText;
  streaming.body.innerHTML = renderMarkdown(streaming.raw);
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy-btn";
  copy.title = "Copy message";
  copy.dataset.raw = streaming.raw;
  copy.innerHTML = COPY_ICON_SVG + "<span>Copy</span>";
  actions.appendChild(copy);
  streaming.col.appendChild(actions);
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

export function addAgentMarkdown(text) {
  hideTyping();
  const col = agentRow();
  const body = document.createElement("div");
  body.className = "agent-body md";
  body.innerHTML = renderMarkdown(text);
  col.appendChild(body);
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
    for (const key of ["command", "path", "url", "query", "key", "name"]) {
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

export function resolveToolCard(id, name, ok, content, attachments = []) {
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
  renderAttachments(t.body, attachments);
  scrollToBottom();
}

// Forget unresolved cards (e.g. a replayed crashed turn): a later result must
// not pair with them.
export function resetPendingTools() {
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
  abandonStreaming();
  threadEl.textContent = "";
  pendingTools = [];
  typingEl = null;
  stick = true;
  jumpBtn.hidden = true;
}

attachCopyHandler(threadEl);
