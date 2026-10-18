// Shared note/todo/plugin rendering. Untrusted content only uses textContent.
const english = (key, values = {}) => {
  const strings = {
    "note.compact_one": "1 earlier message",
    "note.compact_some": "some earlier messages",
    "note.compact_many": "{count} earlier messages",
    "note.compacted": "Context compacted: summarized {label} ({before} → {after} tokens).",
    "plugin.failed": "hook failed",
    "plugin.before_tool.denied": "denied a tool call",
    "plugin.before_tool.asked": "asked for approval",
    "plugin.after_tool.modified": "adjusted a tool result",
    "plugin.user_message.modified": "added context to the message",
    "plugin.user_message.blocked": "blocked the message",
  };
  return (strings[key] || key).replace(/\{(\w+)\}/g, (_, name) => values[name] ?? `{${name}}`);
};

export function createNotes({ row, scrollToBottom, t = english } = {}) {
  const NOTE_ICONS = { skill: "⚙", retry: "⟲", compact: "⊞", error: "⚠", system: "" };

  function tokenEstimate(value) {
    const n = Number(value);
    return Number.isFinite(n) ? `~${Math.max(0, Math.round(n)).toLocaleString()}` : "~?";
  }

  function compactNote(msg) {
    const count = Number(msg.summarized_messages);
    const rounded = Number.isFinite(count) ? Math.max(0, Math.round(count)) : null;
    const label =
      rounded === 1
        ? t("note.compact_one")
        : rounded === null
          ? t("note.compact_some")
          : t("note.compact_many", { count: rounded.toLocaleString() });
    return t("note.compacted", {
      label,
      before: tokenEstimate(msg.before_tokens),
      after: tokenEstimate(msg.after_tokens),
    });
  }

  // A todo_write checklist. Content is model text: rendered via textContent only.
  const TODO_MARKS = { completed: "✓", in_progress: "▸", pending: "○" };

  function addTodoCard(todos, title) {
    const r = row("event");
    const card = document.createElement("div");
    card.className = "note todos";
    const head = document.createElement("div");
    head.className = "todos-head";
    head.textContent = title;
    card.appendChild(head);
    const list = document.createElement("ul");
    list.className = "todos-list";
    for (const item of Array.isArray(todos) ? todos : []) {
      const status = TODO_MARKS[item?.status] ? item.status : "pending";
      const li = document.createElement("li");
      li.className = `todo ${status}`;
      const mark = document.createElement("span");
      mark.className = "todo-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = TODO_MARKS[status];
      const text = document.createElement("span");
      text.textContent = String(item?.content ?? "");
      li.append(mark, text);
      list.appendChild(li);
    }
    card.appendChild(list);
    r.appendChild(card);
    scrollToBottom();
  }

  function todoCounts(todos) {
    const items = Array.isArray(todos) ? todos : [];
    return { done: items.filter((i) => i?.status === "completed").length, total: items.length };
  }

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

  function pluginActionText(msg) {
    if (msg.action === "failed") return t("plugin.failed");
    const key = `plugin.${msg.hook}.${msg.action}`;
    const text = t(key);
    return text === key ? `${msg.hook} · ${msg.action}` : text;
  }

  // A plugin notice: the plugin's name, LingCore's framing of what it did, and the
  // plugin's own words verbatim. The hook and action stay available as a tooltip.
  // LingCore follows every blocked input with an Error naming the plugin and its
  // reason, so that notice is not drawn twice.
  const PLUGIN_TEXT_FROM_CORE = new Set(["Added user context"]);

  function isRedundantPluginNote(msg) {
    return msg.hook === "user_message" && msg.action === "blocked";
  }

  function addPluginNote(msg) {
    if (isRedundantPluginNote(msg)) return;
    const r = row("event");
    const note = document.createElement("div");
    note.className = `note plugin${msg.action === "failed" ? " failed" : ""}`;
    note.title = `${msg.plugin} · ${msg.hook} · ${msg.action}`;
    const name = document.createElement("span");
    name.className = "note-plugin";
    name.textContent = msg.plugin;
    const action = document.createElement("span");
    action.className = "note-plugin-action";
    action.textContent = pluginActionText(msg);
    note.append(name, action);
    if (msg.message && !PLUGIN_TEXT_FROM_CORE.has(msg.message)) {
      const text = document.createElement("span");
      text.className = "note-plugin-text";
      text.textContent = msg.message;
      note.appendChild(text);
    }
    r.appendChild(note);
    scrollToBottom();
  }

  return { compactNote, todoCounts, pluginActionText, addNote, addTodoCard, addPluginNote, isRedundantPluginNote };
}
