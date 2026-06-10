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

(() => {
  "use strict";

  const messagesEl = document.getElementById("messages");
  const formEl = document.getElementById("composer");
  const inputEl = document.getElementById("input");
  const sendEl = document.getElementById("send");
  const statusDot = document.getElementById("status-dot");
  const agentInfo = document.getElementById("agent-info");
  const confirmEl = document.getElementById("confirm");
  const confirmCmd = document.getElementById("confirm-cmd");
  const confirmAllow = document.getElementById("confirm-allow");
  const confirmDeny = document.getElementById("confirm-deny");
  const sessionListEl = document.getElementById("session-list");
  const newChatEl = document.getElementById("new-chat");

  let ws = null;
  let currentAgentMsg = null; // the assistant bubble currently being streamed
  let reconnectTimer = null;
  let sessionId = (location.hash || "").replace(/^#/, "") || null;
  let switching = false; // intentional reconnect to another session
  let suppressReconnect = false; // session open in another tab

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.textContent = text;
    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addEvent(cls, text) {
    const el = document.createElement("div");
    el.className = `event ${cls}`;
    el.textContent = text;
    messagesEl.appendChild(el);
    scrollToBottom();
  }

  function clearMessages() {
    messagesEl.textContent = "";
    currentAgentMsg = null;
  }

  function setConnected(connected) {
    statusDot.classList.toggle("connected", connected);
    statusDot.title = connected ? "connected" : "disconnected";
    sendEl.disabled = !connected;
  }

  function short(value, n = 300) {
    const s = typeof value === "string" ? value : JSON.stringify(value);
    return s.length > n ? s.slice(0, n) + " …" : s;
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
      return;
    }
    if (!data.enabled) {
      renderSessionsDisabled(data.notice);
      return;
    }
    renderSessionList(data.sessions);
  }

  function renderSessionsDisabled(notice) {
    sessionListEl.textContent = "";
    const note = document.createElement("div");
    note.className = "sidebar-note";
    note.textContent =
      notice || "session history is off for this profile (sessions.enabled: false)";
    sessionListEl.appendChild(note);
  }

  function renderSessionList(sessions) {
    sessionListEl.textContent = "";
    for (const s of sessions) {
      const item = document.createElement("div");
      item.className = "session-item" + (s.id === sessionId ? " active" : "");

      const title = document.createElement("div");
      title.className = "session-title";
      title.textContent = s.title || "(untitled)";

      const time = document.createElement("div");
      time.className = "session-time";
      time.textContent = `${relTime(s.updated_at)} · ${s.message_count} msgs`;

      const del = document.createElement("button");
      del.className = "session-delete";
      del.title = "Delete session";
      del.textContent = "×";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });

      item.append(title, time, del);
      item.addEventListener("click", () => switchSession(s.id));
      sessionListEl.appendChild(item);
    }
  }

  async function fetchHistory(id) {
    let res;
    try {
      res = await fetch(`/api/sessions/${id}`);
    } catch {
      return;
    }
    if (!res.ok) return; // 404: fresh id, nothing stored yet
    const data = await res.json();
    renderHistory(data.messages || []);
  }

  function renderHistory(messages) {
    // Stored-message shapes (see server._stored_to_display), not the
    // streaming event shapes handle() deals in.
    for (const m of messages) {
      if (m.role === "user") {
        addMessage("user", m.text);
      } else if (m.role === "assistant") {
        if (m.text) addMessage("agent", m.text);
        for (const tc of m.tool_calls || []) {
          addEvent("tool-call", `→ ${tc.name}(${short(tc.arguments)})`);
        }
      } else if (m.role === "tool") {
        addEvent(`tool-result ${m.ok ? "ok" : "err"}`, `← ${m.name}: ${short(m.content)}`);
      }
    }
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
    sessionId = null;
    history.replaceState(null, "", location.pathname);
    reconnectNow();
  }

  async function deleteSession(id) {
    let res;
    try {
      res = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    } catch {
      return;
    }
    if (res.status === 409) {
      addEvent("error", "close this chat first — it is the open session (use ＋ New chat)");
      return;
    }
    refreshSessions();
  }

  // --- event stream ---------------------------------------------------------

  function handle(msg) {
    switch (msg.type) {
      case "hello": {
        agentInfo.textContent = `${msg.agent} · ${msg.model} · ${msg.workspace}`;
        sessionId = msg.session || null; // server is authoritative
        if (sessionId) history.replaceState(null, "", `#${sessionId}`);
        else history.replaceState(null, "", location.pathname);
        clearMessages();
        if (sessionId) fetchHistory(sessionId);
        refreshSessions();
        break;
      }
      case "session_busy":
        suppressReconnect = true;
        addEvent("error", "this session is open in another tab — close it there, or pick another session");
        break;
      case "text":
        if (!currentAgentMsg) currentAgentMsg = addMessage("agent", "");
        currentAgentMsg.textContent += msg.text;
        scrollToBottom();
        break;
      case "tool_call":
        currentAgentMsg = null;
        addEvent("tool-call", `→ ${msg.name}(${short(msg.arguments)})`);
        break;
      case "tool_result":
        addEvent(`tool-result ${msg.ok ? "ok" : "err"}`, `← ${msg.name}: ${short(msg.content)}`);
        break;
      case "skill":
        addEvent("skill", `⚙ skill ${msg.active ? "activated" : "deactivated"}: ${msg.name}`);
        break;
      case "confirm":
        showConfirm(msg.command);
        break;
      case "final":
        // Streamed text already rendered; ensure a bubble exists if Final
        // arrived without prior deltas.
        if (!currentAgentMsg && msg.text) addMessage("agent", msg.text);
        currentAgentMsg = null;
        break;
      case "turn_end":
        currentAgentMsg = null;
        refreshSessions(); // titles / counts / ordering may have changed
        break;
      case "error":
        currentAgentMsg = null;
        addEvent("error", `error: ${msg.message}`);
        break;
    }
  }

  function showConfirm(command) {
    confirmCmd.textContent = command;
    confirmEl.classList.remove("hidden");
  }

  function answerConfirm(approved) {
    confirmEl.classList.add("hidden");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "confirm_response", approved }));
    }
  }

  confirmAllow.addEventListener("click", () => answerConfirm(true));
  confirmDeny.addEventListener("click", () => answerConfirm(false));
  newChatEl.addEventListener("click", newChat);

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const query = sessionId ? `?session=${encodeURIComponent(sessionId)}` : "";
    ws = new WebSocket(`${proto}://${location.host}/ws${query}`);

    ws.addEventListener("open", () => {
      setConnected(true);
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handle(msg);
    });
    ws.addEventListener("close", () => {
      setConnected(false);
      currentAgentMsg = null;
      if (suppressReconnect) return;
      if (switching) {
        switching = false;
        connect();
        return;
      }
      // Auto-reconnect with a small delay; ?session= makes it a resume.
      if (!reconnectTimer) reconnectTimer = setTimeout(connect, 1500);
    });
    ws.addEventListener("error", () => ws.close());
  }

  function send() {
    const text = inputEl.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    addMessage("user", text);
    currentAgentMsg = null;
    ws.send(JSON.stringify({ type: "user", text }));
    inputEl.value = "";
    autosize();
  }

  formEl.addEventListener("submit", (e) => { e.preventDefault(); send(); });

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + "px";
  }
  inputEl.addEventListener("input", autosize);

  setConnected(false);
  connect();
  refreshSessions();
})();
