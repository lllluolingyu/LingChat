// LingChat browser client.
//
// Opens a WebSocket to the bridge, renders the agent's event stream, and
// answers the shell-confirmation round-trip. Plain ES modules-free JS — no
// build step. Message protocol mirrors lingchat/server.py:_event_to_msg.
//
// Sessions: the current session id lives in the URL hash and is sent as
// ?session= on (re)connect, so a reload or server restart resumes the same
// stored conversation instead of silently starting a fresh agent. The
// sidebar lists stored sessions from GET /api/sessions; "hello" is
// authoritative for which session this socket actually attached to. When the
// profile can't persist, the sidebar stays visible and shows the reason.
//
// Rendering: assistant text is markdown. The renderer is hand-rolled and
// escape-first — every character of model/user/tool text is HTML-escaped
// before any tags are introduced, links are restricted to http(s), and
// innerHTML only ever receives the renderer's own output.

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const messagesEl = $("messages");
  const threadEl = $("thread");
  const formEl = $("composer");
  const inputEl = $("input");
  const sendEl = $("send");
  const attachEl = $("attach");
  const fileInputEl = $("file-input");
  const attachmentTrayEl = $("attachment-tray");
  const statusPill = $("status-pill");
  const statusText = $("status-text");
  const agentChip = $("agent-chip");
  const chatTitleEl = $("chat-title");
  const connBanner = $("conn-banner");
  const confirmEl = $("confirm");
  const confirmCmd = $("confirm-cmd");
  const confirmAllow = $("confirm-allow");
  const confirmDeny = $("confirm-deny");
  const sessionListEl = $("session-list");
  const newChatEl = $("new-chat");
  const themeToggle = $("theme-toggle");
  const themeLabel = $("theme-label");
  const menuBtn = $("menu-btn");
  const sidebarClose = $("sidebar-close");
  const backdrop = $("backdrop");
  const jumpBtn = $("jump-bottom");

  // Focusing the input pops the on-screen keyboard on touch devices — only
  // auto-focus where a hardware pointer/keyboard is the norm.
  const autoFocus = matchMedia("(hover: hover)").matches;
  const focusInput = () => {
    if (autoFocus) inputEl.focus();
  };

  let ws = null;
  let connected = false;
  let everConnected = false;
  let streaming = null; // {body, raw, rafId, actions} for the assistant reply in flight
  let reconnectTimer = null;
  let sessionId = (location.hash || "").replace(/^#/, "") || null;
  let switching = false; // intentional reconnect to another session
  let suppressReconnect = false; // session open in another tab
  let pendingTools = []; // [{name, card}] tool calls awaiting their result
  let typingEl = null;
  let stick = true; // keep the view glued to the newest message
  let agentName = "the agent";
  let modelName = "";
  let pendingAttachments = [];

  const MAX_ATTACHMENTS = 4;
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
  const MAX_FILE_BYTES = 10 * 1024 * 1024;
  const ALLOWED_MEDIA = new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
  ]);

  // --- markdown ------------------------------------------------------------

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

  // Inline transforms over already-escaped text. Code spans are pulled out
  // first so emphasis/link syntax inside them survives untouched.
  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (_, c) => {
      codes.push(c);
      return `\x00${codes.length - 1}\x00`;
    });
    s = s.replace(
      /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    );
    s = s.replace(/\*\*([^*\n](?:[^*\n]*[^*\n])?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    s = s.replace(/(^|[^\w&])_([^_\n]+)_(?=[^\w]|$)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return s.replace(/\x00(\d+)\x00/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function codeBlockHtml(lang, code) {
    return (
      '<div class="codeblock"><div class="codeblock-head">' +
      `<span class="codeblock-lang">${esc(lang || "text")}</span>` +
      '<button class="copy-btn" type="button" title="Copy code">' +
      '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>' +
      "<span>Copy</span></button></div>" +
      `<pre><code>${esc(code)}</code></pre></div>`
    );
  }

  const LIST_RE = /^(\s*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$/;
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function buildList(items) {
    // items: [{depth, ordered, text}] — emit nested <ul>/<ol> via a tag stack.
    let html = "";
    const stack = [];
    for (const it of items) {
      while (stack.length > it.depth + 1) html += `</li></${stack.pop()}>`;
      if (stack.length === it.depth + 1) {
        html += "</li><li>";
      } else {
        while (stack.length < it.depth + 1) {
          const tag = it.ordered ? "ol" : "ul";
          html += `<${tag}><li>`;
          stack.push(tag);
        }
      }
      html += inline(esc(it.text));
    }
    while (stack.length) html += `</li></${stack.pop()}>`;
    return html;
  }

  function renderMarkdown(src) {
    const lines = src.split("\n");
    let html = "";
    let para = [];
    const flush = () => {
      if (para.length) {
        html += `<p>${para.map((l) => inline(esc(l))).join("<br>")}</p>`;
        para = [];
      }
    };

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^\s*```\s*(\S*)\s*$/);
      if (fence) {
        flush();
        i++;
        const buf = [];
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++; // closing fence (or EOF while streaming — render what we have)
        html += codeBlockHtml(fence[1], buf.join("\n"));
        continue;
      }

      if (/^\s*$/.test(line)) {
        flush();
        i++;
        continue;
      }

      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flush();
        const n = h[1].length;
        html += `<h${n}>${inline(esc(h[2]))}</h${n}>`;
        i++;
        continue;
      }

      if (/^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flush();
        html += "<hr>";
        i++;
        continue;
      }

      if (/^\s*>/.test(line)) {
        flush();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        html += `<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`;
        continue;
      }

      const li = line.match(LIST_RE);
      if (li) {
        flush();
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(LIST_RE);
          if (!m) break;
          const indent = m[1].replace(/\t/g, "  ").length;
          items.push({
            depth: Math.min(Math.floor(indent / 2), 3),
            ordered: m[3] !== undefined,
            text: m[4],
          });
          i++;
        }
        html += buildList(items);
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
        flush();
        const splitRow = (l) =>
          l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
        const head = splitRow(line);
        i += 2;
        let table =
          "<table><thead><tr>" +
          head.map((c) => `<th>${inline(esc(c))}</th>`).join("") +
          "</tr></thead><tbody>";
        while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) {
          const cells = splitRow(lines[i]);
          table +=
            "<tr>" +
            head.map((_, c) => `<td>${inline(esc(cells[c] ?? ""))}</td>`).join("") +
            "</tr>";
          i++;
        }
        html += table + "</tbody></table>";
        continue;
      }

      para.push(line);
      i++;
    }
    flush();
    return html;
  }

  // --- clipboard (event-delegated for all copy buttons) ---------------------

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* nothing left to try */ }
      ta.remove();
    }
    const label = btn.querySelector("span");
    btn.classList.add("copied");
    if (label) label.textContent = "Copied";
    setTimeout(() => {
      btn.classList.remove("copied");
      if (label) label.textContent = "Copy";
    }, 1400);
  }

  threadEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    if (btn.dataset.raw !== undefined) {
      copyText(btn.dataset.raw, btn);
      return;
    }
    const block = btn.closest(".codeblock");
    const code = block && block.querySelector("pre code");
    if (code) copyText(code.textContent, btn);
  });

  // --- scrolling -------------------------------------------------------------

  function nearBottom() {
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 90;
  }

  function scrollToBottom(force = false) {
    if (!force && !stick) return;
    messagesEl.scrollTop = messagesEl.scrollHeight;
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

  // --- thread rendering -------------------------------------------------------

  const AVATAR_SVG =
    '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 6.5l1.9 6 6.1 1.9-6.1 1.9-1.9 6-1.9-6-6.1-1.9 6.1-1.9z" fill="white"/></svg>';

  function removeEmptyState() {
    const el = threadEl.querySelector(".empty-state");
    if (el) el.remove();
  }

  function showEmptyState() {
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

  function attachmentLabel(a) {
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

  function addUserMessage(text, attachments = [], synthetic = false) {
    const r = row(synthetic ? "event" : "user");
    const bubble = document.createElement("div");
    bubble.className = synthetic ? "note system media-note" : "bubble-user";
    if (text) bubble.appendChild(document.createTextNode(text));
    else bubble.appendChild(document.createTextNode("Attached media"));
    renderAttachments(bubble, attachments);
    r.appendChild(bubble);
    scrollToBottom();
  }

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

  function appendAgentText(text) {
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

  function finalizeAgentMessage() {
    if (!streaming) return;
    if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
    streaming.body.innerHTML = renderMarkdown(streaming.raw);
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-btn";
    copy.title = "Copy message";
    copy.dataset.raw = streaming.raw;
    copy.innerHTML =
      '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg><span>Copy</span>';
    actions.appendChild(copy);
    streaming.col.appendChild(actions);
    streaming = null;
    scrollToBottom();
  }

  // The in-flight reply was lost mid-stream: render what arrived, struck
  // through, with no copy action — the retry supersedes it.
  function discardAgentMessage() {
    if (!streaming) return;
    if (streaming.rafId) cancelAnimationFrame(streaming.rafId);
    streaming.body.innerHTML = renderMarkdown(streaming.raw);
    streaming.body.classList.add("discarded");
    streaming = null;
  }

  function addAgentMarkdown(text) {
    hideTyping();
    const col = agentRow();
    const body = document.createElement("div");
    body.className = "agent-body md";
    body.innerHTML = renderMarkdown(text);
    col.appendChild(body);
    scrollToBottom();
    return body;
  }

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

  function addToolCard(name, args) {
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
    pendingTools.push({ name, status, body, summaryEl: summary });
    scrollToBottom();
    return card;
  }

  function resolveToolCard(name, ok, content, attachments = []) {
    const idx = pendingTools.findIndex((t) => t.name === name);
    if (idx >= 0) {
      const t = pendingTools.splice(idx, 1)[0];
      t.status.innerHTML = ok ? OK_ICON : ERR_ICON;
      t.body.appendChild(toolSection(ok ? "Result" : "Error", content, !ok));
      renderAttachments(t.body, attachments);
    } else {
      // A result with no visible call (shouldn't happen, but render honestly).
      addToolCard(name, null);
      const t = pendingTools.pop();
      t.status.innerHTML = ok ? OK_ICON : ERR_ICON;
      t.body.appendChild(toolSection(ok ? "Result" : "Error", content, !ok));
      renderAttachments(t.body, attachments);
    }
    scrollToBottom();
  }

  const NOTE_ICONS = { skill: "⚙", retry: "⟲", error: "⚠", system: "" };

  function addNote(kind, text) {
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

  function showTyping() {
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

  function hideTyping() {
    if (typingEl) {
      typingEl.remove();
      typingEl = null;
    }
  }

  function clearThread() {
    threadEl.textContent = "";
    streaming = null;
    pendingTools = [];
    typingEl = null;
    stick = true;
    jumpBtn.hidden = true;
  }

  // --- connection status -------------------------------------------------------

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
    connected = state === "connected";
    updateSendState();
  }

  function updateSendState() {
    sendEl.disabled = !connected || (!inputEl.value.trim() && !pendingAttachments.length);
  }

  function setChatTitle(title) {
    const t = title || "New chat";
    chatTitleEl.textContent = t;
    chatTitleEl.title = t;
    document.title = title ? `${title} · LingChat` : "LingChat";
  }

  function relTime(iso) {
    const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  // --- session sidebar ----------------------------------------------------

  async function refreshSessions() {
    let data;
    try {
      data = await (await fetch("/api/sessions")).json();
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
  async function loadThread() {
    const data = await refreshSessions();
    if (!data) {
      // Listing failed (transient?) — fall back to probing directly.
      if (sessionId) fetchHistory(sessionId);
      else showEmptyState();
      return;
    }
    const known =
      data.enabled && sessionId && data.sessions.some((s) => s.id === sessionId);
    if (known) fetchHistory(sessionId);
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
      await fetch(`/api/sessions/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
    } catch {
      return;
    }
    refreshSessions();
  }

  async function fetchHistory(id) {
    let res;
    try {
      res = await fetch(`/api/sessions/${encodeURIComponent(id)}`);
    } catch {
      showEmptyState();
      return;
    }
    if (!res.ok) {
      // 404: fresh id, nothing stored yet.
      showEmptyState();
      return;
    }
    const data = await res.json();
    renderHistory(data.messages || []);
    if (data.title) setChatTitle(data.title);
  }

  function renderHistory(messages) {
    // Stored-message shapes (see server._stored_to_display), not the
    // streaming event shapes handle() deals in.
    for (const m of messages) {
      if (m.role === "user") {
        addUserMessage(m.text, m.attachments || [], m.name === "media");
      } else if (m.role === "assistant") {
        if (m.text) addAgentMarkdown(m.text);
        for (const tc of m.tool_calls || []) {
          addToolCard(tc.name, tc.arguments);
        }
      } else if (m.role === "tool") {
        resolveToolCard(m.name, m.ok, m.content, m.attachments || []);
      }
    }
    pendingTools = []; // anything unresolved belongs to a crashed turn; don't pair it later
    if (!messages.length) showEmptyState();
    scrollToBottom(true);
  }

  function reconnectNow() {
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

  function switchSession(id) {
    if (id === sessionId) return;
    sessionId = id;
    history.replaceState(null, "", `#${id}`);
    reconnectNow();
  }

  function newChat() {
    closeSidebar();
    sessionId = null;
    history.replaceState(null, "", location.pathname);
    reconnectNow();
    focusInput();
  }

  async function deleteSession(id) {
    let res;
    try {
      res = await fetch(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch {
      return;
    }
    if (res.status === 409) {
      addNote("error", "Close this chat first — it is the open session (use New chat).");
      return;
    }
    refreshSessions();
  }

  // --- event stream ---------------------------------------------------------

  function handle(msg) {
    switch (msg.type) {
      case "hello": {
        agentName = msg.agent || "the agent";
        modelName = msg.model || "";
        agentChip.textContent = `${msg.agent} · ${msg.model}`;
        agentChip.title = `Workspace: ${msg.workspace}`;
        agentChip.hidden = false;
        inputEl.placeholder = `Message ${agentName}…`;
        sessionId = msg.session || null; // server is authoritative
        if (sessionId) history.replaceState(null, "", `#${sessionId}`);
        else history.replaceState(null, "", location.pathname);
        clearThread();
        setChatTitle(msg.title || "");
        loadThread();
        break;
      }
      case "session_busy":
        suppressReconnect = true;
        addNote(
          "error",
          "This session is open in another tab — close it there, or pick another session.",
        );
        break;
      case "text":
        if (!streaming) startAgentMessage();
        appendAgentText(msg.text);
        break;
      case "tool_call":
        finalizeAgentMessage();
        hideTyping();
        addToolCard(msg.name, msg.arguments);
        break;
      case "tool_result":
        resolveToolCard(msg.name, msg.ok, msg.content, msg.attachments || []);
        showTyping(); // the model is reading the result
        break;
      case "skill":
        addNote("skill", `Skill ${msg.active ? "activated" : "deactivated"}: ${msg.name}`);
        break;
      case "stream_retry":
        // The in-flight reply was lost mid-stream; whatever the current
        // bubble holds is void and will be regenerated (possibly differently).
        if (streaming && msg.discarded_chars) discardAgentMessage();
        else streaming = null;
        addNote("retry", `${msg.reason}; retrying (${msg.attempt}/${msg.max_attempts})`);
        showTyping();
        break;
      case "confirm":
        hideTyping();
        showConfirm(msg.command);
        break;
      case "final":
        // Streamed text already rendered; ensure a bubble exists if Final
        // arrived without prior deltas.
        if (streaming) {
          if (msg.text) streaming.raw = msg.text; // authoritative full text
          finalizeAgentMessage();
        } else if (msg.text) {
          addAgentMarkdown(msg.text);
        }
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

  // --- confirm modal ----------------------------------------------------------

  function showConfirm(command) {
    confirmCmd.textContent = command;
    confirmEl.classList.remove("hidden");
    confirmDeny.focus(); // safe default for a stray Enter
  }

  function answerConfirm(approved) {
    confirmEl.classList.add("hidden");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "confirm_response", approved }));
    }
    focusInput();
  }

  confirmAllow.addEventListener("click", () => answerConfirm(true));
  confirmDeny.addEventListener("click", () => answerConfirm(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !confirmEl.classList.contains("hidden")) {
      answerConfirm(false);
    }
  });

  // --- websocket ----------------------------------------------------------------

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const query = sessionId ? `?session=${encodeURIComponent(sessionId)}` : "";
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
      handle(msg);
    });
    sock.addEventListener("close", () => {
      if (stale()) return;
      streaming = null;
      hideTyping();
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

  // --- composer -------------------------------------------------------------------

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

  async function addFiles(files) {
    for (const file of files) {
      if (pendingAttachments.length >= MAX_ATTACHMENTS) {
        addNote("error", `You can attach at most ${MAX_ATTACHMENTS} files per message.`);
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
    if ((!text && !pendingAttachments.length) || !ws || ws.readyState !== WebSocket.OPEN) return;
    const attachments = pendingAttachments;
    addUserMessage(text, attachments);
    finalizeAgentMessage();
    stick = true;
    ws.send(JSON.stringify({ type: "user", text, attachments }));
    inputEl.value = "";
    pendingAttachments = [];
    renderAttachmentTray();
    autosize();
    updateSendState();
    showTyping();
    scrollToBottom(true);
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

  // --- sidebar drawer (mobile) ------------------------------------------------------

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
  newChatEl.addEventListener("click", newChat);

  // --- boot -------------------------------------------------------------------------

  syncThemeLabel();
  setStatus("connecting");
  connect();
  refreshSessions();
  focusInput();
})();
