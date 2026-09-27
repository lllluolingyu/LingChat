/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// Interface language: the string table, the lookup, and the static-DOM pass.
//
// One table, keyed by string id with every language beside it, so a missing
// translation is visible in the diff that adds the string rather than found
// later in the running UI. `t()` falls back to English and then to the key
// itself, so an untranslated string degrades to readable text instead of an
// empty element.
//
// The language lives in localStorage next to the theme and is applied the same
// way: a pre-paint read in the page head sets <html lang>, which also decides
// which face the CJK fallback in --sans/--serif resolves to. A consumer (see
// Agent-Chat-GUI) adds its own strings with register() so there is one store
// and one toggle across every page that loads this module.

const STORAGE_KEY = "agentgui-lang";

export const DEFAULT_LANG = "zh";
export const LANGS = ["zh", "en"];
export const LANG_NAMES = { zh: "中文", en: "English" };

// key: { en, zh }. Interpolation is `{name}`; see t().
const STRINGS = {
  // --- chrome ---------------------------------------------------------------
  "app.sessions": { en: "Sessions", zh: "会话" },
  "app.stored_sessions": { en: "Stored sessions", zh: "已存对话" },
  "app.close_sidebar": { en: "Close sidebar", zh: "收起侧栏" },
  "app.open_sidebar": { en: "Open sidebar", zh: "展开侧栏" },
  "app.new_chat": { en: "New chat", zh: "新对话" },
  "app.fork_chat": { en: "Fork chat", zh: "复刻对话" },
  "app.doctor": { en: "Installation checks", zh: "安装检查" },
  "app.doctor_running": { en: "Checking installed agents…", zh: "正在检查已安装的智能体…" },
  "app.close": { en: "Close", zh: "关闭" },
  "app.cancel": { en: "Cancel", zh: "取消" },
  "app.theme_toggle": { en: "Toggle theme", zh: "切换主题" },
  "app.theme_light": { en: "Light theme", zh: "浅色主题" },
  "app.theme_dark": { en: "Dark theme", zh: "深色主题" },
  "app.lang_toggle": { en: "Change interface language", zh: "切换界面语言" },
  "app.auth_hint": {
    en: "Open the token URL printed by agentgui to authenticate.",
    zh: "请打开 agentgui 启动时输出的带令牌地址完成认证。",
  },

  // --- connection -----------------------------------------------------------
  "conn.connected": { en: "Connected", zh: "已连接" },
  "conn.connecting": { en: "Connecting", zh: "连接中" },
  "conn.reconnecting": { en: "Reconnecting", zh: "重连中" },
  "conn.offline": { en: "Offline", zh: "已离线" },
  "conn.lost": {
    en: "Connection lost — reconnecting…",
    zh: "连接已断开 — 正在重连…",
  },

  // --- composer -------------------------------------------------------------
  "composer.attach": { en: "Attach files", zh: "添加附件" },
  "composer.send": { en: "Send", zh: "发送" },
  "composer.send_message": { en: "Send message", zh: "发送消息" },
  "composer.stop": { en: "Stop response", zh: "停止回复" },
  "composer.message": { en: "Message", zh: "消息" },
  "composer.placeholder": { en: "Message the agent…", zh: "给智能体发消息…" },
  "composer.placeholder_named": { en: "Message {name}…", zh: "给 {name} 发消息…" },
  "composer.hint_send": { en: "to send", zh: "发送" },
  "composer.hint_newline": { en: "for a new line", zh: "换行" },
  "composer.jump_latest": { en: "Scroll to latest", zh: "回到最新消息" },
  "composer.too_many": {
    en: "You can attach at most {count} files per message.",
    zh: "每条消息最多只能附带 {count} 个文件。",
  },
  "composer.too_large": {
    en: "Attachments exceed the {mb}MB total limit per message.",
    zh: "附件总大小超出每条消息 {mb}MB 的上限。",
  },
  "composer.images_only": {
    en: "This backend supports image attachments only.",
    zh: "此后端仅支持图片附件。",
  },
  "composer.no_images": {
    en: "This backend does not support images.",
    zh: "此后端不支持图片。",
  },

  // --- approval modal -------------------------------------------------------
  "approval.title": { en: "Approve action?", zh: "允许该操作？" },
  "approval.sub": {
    en: "The agent wants to execute this in the workspace.",
    zh: "智能体请求在工作区中执行以下操作。",
  },
  "approval.deny": { en: "Deny", zh: "拒绝" },
  "approval.allow_once": { en: "Allow once", zh: "仅此一次" },
  "approval.allow_session": { en: "Allow for session", zh: "本次会话均允许" },

  // --- new-chat dialog ------------------------------------------------------
  "newchat.intro": {
    en: "Choose an agent, workspace and permission level for this conversation.",
    zh: "为这次对话选择智能体、工作区与权限级别。",
  },
  "newchat.model": { en: "Model", zh: "模型" },
  "newchat.workspace": { en: "Workspace directory", zh: "工作区目录" },
  "newchat.autonomy": { en: "Autonomy", zh: "自主级别" },
  "newchat.autonomy_ask": {
    en: "Ask before writing (default)",
    zh: "写入前询问（默认）",
  },
  "newchat.autonomy_edit": { en: "Edit the workspace", zh: "可直接修改工作区" },
  "newchat.create": { en: "Create chat", zh: "创建对话" },
  "newchat.failed": {
    en: "Check the model and workspace directory.",
    zh: "请检查模型与工作区目录。",
  },
  "backend.claude": { en: "Claude Code", zh: "Claude Code" },
  "backend.codex": { en: "Codex", zh: "Codex" },
  "backend.lingcore": {
    en: "LingCore · cost-effective",
    zh: "LingCore · 经济型",
  },

  // --- session list ---------------------------------------------------------
  "sessions.empty": { en: "No conversations yet", zh: "还没有对话" },
  "sessions.disabled": {
    en: "session history is off for this profile (sessions.enabled: false)",
    zh: "该配置未启用会话历史（sessions.enabled: false）",
  },
  "sessions.agent": { en: "agent", zh: "智能体" },
  "sessions.rename": { en: "Rename", zh: "重命名" },
  "sessions.name": { en: "Session name", zh: "对话名称" },
  "sessions.delete": { en: "Delete", zh: "删除" },
  "sessions.delete_confirm": { en: "Delete this chat?", zh: "删除这个对话？" },
  "sessions.delete_again": { en: "Click again to confirm", zh: "再点一次确认" },
  "sessions.delete_open": {
    en: "Close this chat first — it is the open session (use New chat).",
    zh: "请先离开这个对话 — 它是当前打开的会话（点「新对话」）。",
  },
  "sessions.messages": { en: "{count} msgs", zh: "{count} 条消息" },
  "sessions.fork_failed": {
    en: "Could not fork this conversation.",
    zh: "无法复刻这个对话。",
  },
  "sessions.fork_bad_json": {
    en: "The fork response was not valid JSON.",
    zh: "复刻请求返回的不是合法 JSON。",
  },
  "sessions.fork_no_id": {
    en: "The fork response did not include a session id.",
    zh: "复刻请求返回的结果缺少会话 id。",
  },

  // --- relative time and date groups ----------------------------------------
  "time.now": { en: "just now", zh: "刚刚" },
  "time.minutes": { en: "{n}m ago", zh: "{n} 分钟前" },
  "time.hours": { en: "{n}h ago", zh: "{n} 小时前" },
  "time.days": { en: "{n}d ago", zh: "{n} 天前" },
  "group.today": { en: "Today", zh: "今天" },
  "group.yesterday": { en: "Yesterday", zh: "昨天" },
  "group.week": { en: "Previous 7 days", zh: "过去 7 天" },
  "group.month": { en: "Previous 30 days", zh: "过去 30 天" },
  "group.older": { en: "Older", zh: "更早" },

  // --- thread ---------------------------------------------------------------
  "thread.the_agent": { en: "the agent", zh: "智能体" },
  "thread.empty_title": { en: "Chat with {name}", zh: "与 {name} 对话" },
  "thread.empty_sub": {
    en: "Messages and tool activity will appear here.",
    zh: "消息与工具调用会显示在这里。",
  },
  "thread.empty_sub_model": {
    en: "Running on {model}. Messages and tool activity will appear here.",
    zh: "当前使用 {model}。消息与工具调用会显示在这里。",
  },
  "thread.attachment": { en: "attachment", zh: "附件" },
  "thread.attached_media": { en: "Attached media", zh: "附带的媒体文件" },
  "thread.download": { en: "Download", zh: "下载" },
  "thread.download_failed": { en: "Failed", zh: "失败" },
  "thread.edit": { en: "Edit", zh: "编辑" },
  "thread.edit_message": { en: "Edit message", zh: "编辑消息" },
  "thread.save_regenerate": { en: "Save & regenerate", zh: "保存并重新生成" },
  "thread.copy": { en: "Copy", zh: "复制" },
  "thread.copied": { en: "Copied", zh: "已复制" },
  "thread.copy_message": { en: "Copy message", zh: "复制消息" },
  "thread.copy_code": { en: "Copy code", zh: "复制代码" },
  "thread.fork": { en: "Fork", zh: "复刻" },
  "thread.fork_here": {
    en: "Fork conversation from here",
    zh: "从这里复刻对话",
  },
  "thread.fork_regenerate": {
    en: "Fork and regenerate from this message",
    zh: "复刻并从这条消息重新生成",
  },
  "thread.thinking": { en: "Thinking", zh: "思考过程" },
  "thread.workspace": { en: "Workspace: {path}", zh: "工作区：{path}" },

  // --- tool cards -----------------------------------------------------------
  "tool.arguments": { en: "Arguments", zh: "参数" },
  "tool.result": { en: "Result", zh: "结果" },
  "tool.error": { en: "Error", zh: "错误" },
  "tool.stopped": { en: "Stopped", zh: "已停止" },
  "tool.cancelled": { en: "Cancelled by user", zh: "已被用户取消" },

  // --- notes ----------------------------------------------------------------
  "note.turn_busy": {
    en: "A turn is already running. Stop it before sending another message.",
    zh: "当前还有一轮回复在进行，请先停止再发送新消息。",
  },
  "note.session_busy": {
    en: "This session is open in another tab — close it there, or pick another session.",
    zh: "这个会话已在另一个标签页打开 — 请关闭那边，或选择其他会话。",
  },
  "note.skill_on": { en: "Skill activated: {name}", zh: "已启用技能：{name}" },
  "note.skill_off": { en: "Skill deactivated: {name}", zh: "已停用技能：{name}" },
  "note.retry": {
    en: "{reason}; retrying ({attempt}/{max})",
    zh: "{reason}；正在重试（{attempt}/{max}）",
  },
  "note.stopped": { en: "Stopped by user", zh: "已被用户停止" },
  "note.edit_failed": {
    en: "The message could not be edited.",
    zh: "这条消息无法编辑。",
  },
  "note.fork_regen_failed": {
    en: "The fork was created, but regeneration could not start.",
    zh: "复刻已创建，但重新生成未能启动。",
  },
  "note.compact_one": { en: "1 earlier message", zh: "1 条较早的消息" },
  "note.compact_many": { en: "{count} earlier messages", zh: "{count} 条较早的消息" },
  "note.compact_some": { en: "some earlier messages", zh: "若干较早的消息" },
  "note.compacted": {
    en: "Context compacted: summarized {label} ({before} → {after} tokens).",
    zh: "上下文已压缩：摘要了{label}（{before} → {after} tokens）。",
  },

  // --- usage chip -----------------------------------------------------------
  "usage.tokens": { en: "{input} in · {output} out", zh: "输入 {input} · 输出 {output}" },
  "usage.context": { en: "{pct}% context", zh: "上下文 {pct}%" },
  "usage.cached": { en: "{count} cached tokens", zh: "缓存 {count} tokens" },
};

/** Merge a consumer's strings in (same `{key: {en, zh}}` shape) and repaint. */
export function register(extra) {
  for (const [key, value] of Object.entries(extra || {})) STRINGS[key] = value;
  apply();
}

function stored() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return LANGS.includes(value) ? value : null;
  } catch {
    return null; // storage unavailable (private mode): the default applies
  }
}

let current = stored() || DEFAULT_LANG;

export function lang() {
  return current;
}

/**
 * Look up `key`, substituting `{name}` placeholders from `vars`.
 *
 * Falls through English to the key itself: an untranslated string shows as
 * readable text rather than blanking the element that holds it.
 */
export function t(key, vars) {
  const entry = STRINGS[key];
  let text = entry ? (entry[current] ?? entry.en ?? key) : key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
  }
  return text;
}

// Subscribers re-render the DOM they built themselves; the static pass below
// only reaches markup that carries a data-i18n attribute.
const listeners = new Set();

export function onLangChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// textContent for data-i18n; the rest set the matching attribute. An element
// whose text sits beside an icon wraps that text in its own <span data-i18n>,
// so nothing here has to preserve sibling nodes.
const ATTRS = [
  ["data-i18n-title", "i18nTitle", "title"],
  ["data-i18n-placeholder", "i18nPlaceholder", "placeholder"],
  ["data-i18n-aria-label", "i18nAriaLabel", "aria-label"],
  ["data-i18n-label", "i18nLabel", "label"],
];

/** Translate every `[data-i18n*]` node under `root` (default: the document). */
export function apply(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const [selector, prop, attr] of ATTRS) {
    for (const el of root.querySelectorAll(`[${selector}]`)) {
      el.setAttribute(attr, t(el.dataset[prop]));
    }
  }
}

export function setLang(code) {
  if (!LANGS.includes(code) || code === current) return;
  current = code;
  try {
    localStorage.setItem(STORAGE_KEY, code);
  } catch { /* private mode — the choice just won't persist */ }
  document.documentElement.lang = code;
  apply();
  for (const fn of listeners) fn(code);
}

/** The language a toggle would move to next (two languages, so: the other). */
export function nextLang() {
  return LANGS[(LANGS.indexOf(current) + 1) % LANGS.length];
}

document.documentElement.lang = current;
