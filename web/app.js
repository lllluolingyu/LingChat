// LingChat browser client.
//
// Opens a WebSocket to the bridge, renders the agent's event stream, and
// answers the shell-confirmation round-trip. Plain ES modules-free JS — no
// build step. Message protocol mirrors lingchat/server.py:_event_to_msg.

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

  let ws = null;
  let currentAgentMsg = null; // the assistant bubble currently being streamed
  let reconnectTimer = null;

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

  function setConnected(connected) {
    statusDot.classList.toggle("connected", connected);
    statusDot.title = connected ? "connected" : "disconnected";
    sendEl.disabled = !connected;
  }

  function short(value, n = 300) {
    const s = typeof value === "string" ? value : JSON.stringify(value);
    return s.length > n ? s.slice(0, n) + " …" : s;
  }

  function handle(msg) {
    switch (msg.type) {
      case "hello":
        agentInfo.textContent = `${msg.agent} · ${msg.model} · ${msg.workspace}`;
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

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

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
      // Auto-reconnect with a small delay.
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
})();
