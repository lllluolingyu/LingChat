// Shared usage rendering: each reply's tokens and finish time beside its
// actions, and the session line under the composer (#session-stats). The
// accounting itself lives in usage.js; this file owns only the DOM.
import { cacheHitPct, compactTokens, createLedger, replyTime } from "./usage.js";

const english = (key, values = {}) => {
  const strings = {
    "usage.breakdown": "{prompt} prompt ({cached} cached) · {output} output tokens",
    "reply.usage": "{total} tok",
    "stats.turns_one": "1 turn",
    "stats.turns": "{count} turns",
    "stats.steps_one": "1 step",
    "stats.steps": "{count} steps",
    "stats.pace": "{turns}, {steps}",
    "stats.rate": "{rate} tok/s",
    "stats.rate_title":
      "Output tokens per second of generation, not counting time spent in tools or waiting for approval",
    "stats.cache_hit": "{pct}% cache hit",
    "stats.context_title": "{pct}% of the context window used",
  };
  return (strings[key] || key).replace(/\{(\w+)\}/g, (_, name) => values[name] ?? `{${name}}`);
};

const USAGE_ICON =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="4" rx="5" ry="2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';

function formatRate(rate) {
  return rate >= 10 ? String(Math.round(rate)) : rate.toFixed(1);
}

/**
 * `row(cls)` appends a thread row (for a reply that left no bubble to annotate);
 * `locale()` names the language reply times are written in.
 */
export function createUsageView({ row, t = english, locale = () => undefined } = {}) {
  const $ = (id) => document.getElementById(id);
  let ledger = createLedger();
  let replyActions = null; // .msg-actions of the newest bubble in the current reply
  let shown = new WeakSet(); // replies whose usage and time are on screen
  let frame = 0;

  function tokensTitle(tokens) {
    return t("usage.breakdown", {
      prompt: tokens.prompt.toLocaleString(),
      cached: tokens.cached.toLocaleString(),
      output: tokens.output.toLocaleString(),
    });
  }

  function replyMeta(reply) {
    const meta = document.createElement("span");
    meta.className = "reply-meta";
    if (reply.metered) {
      const usage = document.createElement("span");
      usage.className = "reply-usage";
      usage.innerHTML = USAGE_ICON;
      usage.append(t("reply.usage", { total: compactTokens(reply.tokens.total) }));
      let title = tokensTitle(reply.tokens);
      if (reply.timing?.modelMs) {
        const rate = reply.tokens.output / (reply.timing.modelMs / 1000);
        title += ` · ${t("stats.rate", { rate: formatRate(rate) })}`;
      }
      usage.title = title;
      meta.appendChild(usage);
    }
    const when = reply.timing?.endedAt ? replyTime(reply.timing.endedAt, locale()) : null;
    if (when) {
      const time = document.createElement("time");
      time.dateTime = reply.timing.endedAt;
      time.textContent = when.short;
      time.title = when.full;
      meta.appendChild(time);
    }
    return meta.childElementCount ? meta : null;
  }

  // Put the closing reply's usage and finish time beside its last bubble's
  // actions; a reply that left no bubble (stopped, failed) gets a line of its own.
  function closeReply() {
    const reply = ledger.current();
    if (!reply || shown.has(reply)) return;
    shown.add(reply);
    const meta = replyMeta(reply);
    if (!meta) return;
    if (replyActions?.isConnected) replyActions.appendChild(meta);
    else row("event reply-meta-row").appendChild(meta);
  }

  // Repaints are batched to one per frame, since a replayed transcript
  // records hundreds of steps in a row.
  function paint() {
    if (!frame) frame = requestAnimationFrame(draw);
  }

  function plural(key, n) {
    return t(n === 1 ? `${key}_one` : key, { count: n.toLocaleString() });
  }

  function draw() {
    frame = 0;
    const el = $("session-stats");
    if (!el) return;
    const s = ledger.summary();
    el.hidden = !s.turns;
    if (!s.turns) return;

    let pace = t("stats.pace", {
      turns: plural("stats.turns", s.turns),
      steps: plural("stats.steps", s.steps),
    });
    if (s.rate != null) pace += ` · ${t("stats.rate", { rate: formatRate(s.rate) })}`;
    $("stat-pace-text").textContent = pace;
    $("stat-pace").title = s.rate != null ? t("stats.rate_title") : "";

    const tokensEl = $("stat-tokens");
    tokensEl.hidden = !s.metered;
    if (s.metered) {
      let text = `${compactTokens(s.tokens.total)} tok`;
      const hit = cacheHitPct(s.tokens);
      if (hit != null) text += ` · ${t("stats.cache_hit", { pct: Math.floor(hit) })}`;
      if (s.costUsd != null) text += ` · $${s.costUsd.toFixed(s.costUsd < 1 ? 4 : 2)}`;
      $("stat-tokens-text").textContent = text;
      tokensEl.title = tokensTitle(s.tokens);
    }

    const contextEl = $("stat-context");
    contextEl.hidden = s.contextPct == null;
    if (s.contextPct != null) {
      const pct = Math.round(s.contextPct);
      $("stat-context-fill").setAttribute("stroke-dasharray", `${s.contextPct} 100`);
      $("stat-context-text").textContent = `${pct}%`;
      contextEl.title = t("stats.context_title", { pct });
    }
  }

  return {
    /** A user message starts the next reply (and closes the one before). */
    beginReply() {
      closeReply();
      ledger.begin();
      replyActions = null;
      paint();
    },
    /** The actions row of a reply bubble just rendered. */
    setReplyActions(actions) {
      replyActions = actions;
    },
    step() {
      ledger.step();
      paint();
    },
    recordUsage(msg) {
      ledger.usage(msg);
      paint();
    },
    recordTiming(msg) {
      ledger.timing(msg);
      paint();
    },
    closeReply,
    reset() {
      ledger = createLedger();
      replyActions = null;
      shown = new WeakSet();
      paint();
    },
  };
}
