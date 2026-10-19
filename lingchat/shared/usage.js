// Usage accounting for one transcript: what each reply spent, what the session
// has spent, and how fast it generated, from `usage` frames (agentgui/usage.py,
// lingchat/server.py) and `timing` frames (lingchat/usage.py). Shared by both
// UIs; no DOM and no strings, so it runs under node's test runner.
//
// The flat usage counters mean different things per backend, and each frame
// says which: `scope: "conversation"` frames carry running totals (Codex), so a
// reply's share is its last total minus the total before it; any other scope
// covers one request or one turn and simply adds up. `cache_in_input` says
// whether `cached`/`cache_write` sit inside `input` or beside it (Claude).

const count = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
};

const ZERO = Object.freeze({ prompt: 0, cached: 0, output: 0, total: 0 });

/** Normalize one usage frame to {prompt, cached, output, total} tokens. */
export function tokenCounts(frame) {
  const input = count(frame?.input);
  const cached = count(frame?.cached);
  const output = count(frame?.output);
  // Frames stored before cache_in_input existed: only a separate cache count
  // can exceed the input it would otherwise be part of.
  const separate =
    frame?.cache_in_input === false || (frame?.cache_in_input == null && cached > input);
  const prompt = separate ? input + cached + count(frame?.cache_write) : input;
  return { prompt, cached, output, total: prompt + output };
}

function plus(a, b) {
  return {
    prompt: a.prompt + b.prompt,
    cached: a.cached + b.cached,
    output: a.output + b.output,
    total: a.total + b.total,
  };
}

// A running total that went backwards (a fork onto a fresh native thread)
// restarted, so everything it reports is new.
function minus(a, b) {
  if (!b) return a;
  const diff = {
    prompt: a.prompt - b.prompt,
    cached: a.cached - b.cached,
    output: a.output - b.output,
    total: a.total - b.total,
  };
  return Object.values(diff).some((n) => n < 0) ? a : diff;
}

/** Share of prompt tokens served from cache, 0–100, or null with no prompt. */
export function cacheHitPct(tokens) {
  if (!tokens.prompt) return null;
  return Math.min(100, (tokens.cached / tokens.prompt) * 100);
}

export function createLedger() {
  const replies = [];
  let current = null;
  let running = null; // the newest conversation-scope total seen
  let steps = 0;
  let contextPct = null;
  let costUsd = null;

  function reply() {
    // Usage ahead of any user message still belongs somewhere.
    if (!current) begin();
    return current;
  }

  /** Start the next reply; returns the one it closes, if any. */
  function begin() {
    const closed = current;
    current = { tokens: ZERO, base: running, metered: false, timing: null, steps: 0 };
    replies.push(current);
    return closed;
  }

  function usage(frame) {
    const r = reply();
    const tokens = tokenCounts(frame);
    if (frame.scope === "conversation") {
      running = tokens;
      r.tokens = minus(tokens, r.base);
    } else {
      r.tokens = plus(r.tokens, tokens);
    }
    r.metered = true;
    if (frame.context_pct != null && Number.isFinite(Number(frame.context_pct))) {
      contextPct = Math.max(0, Math.min(100, Number(frame.context_pct)));
    }
    // Every backend that reports cost reports a session running total.
    if (frame.cost_usd != null && Number.isFinite(Number(frame.cost_usd))) {
      costUsd = Number(frame.cost_usd);
    }
  }

  function timing(frame) {
    const modelMs = count(frame?.model_ms);
    const endedAt = typeof frame?.ended_at === "string" ? frame.ended_at : null;
    reply().timing = { endedAt, modelMs };
  }

  function step() {
    reply().steps += 1;
    steps += 1;
  }

  function summary() {
    let tokens = ZERO;
    let timedOutput = 0;
    let modelMs = 0;
    for (const r of replies) {
      tokens = plus(tokens, r.tokens);
      if (r.metered && r.timing?.modelMs) {
        timedOutput += r.tokens.output;
        modelMs += r.timing.modelMs;
      }
    }
    return {
      turns: replies.length,
      steps,
      tokens,
      metered: replies.some((r) => r.metered),
      rate: modelMs ? timedOutput / (modelMs / 1000) : null,
      contextPct,
      costUsd,
    };
  }

  return { begin, usage, timing, step, summary, current: () => current };
}

/** 950 → "950", 12_345 → "12.3K", 14_200_000 → "14.2M". */
export function compactTokens(value) {
  return new Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(count(value));
}

/** Clock time for today's replies; the date as well for anything older. */
export function replyTime(iso, locale, now = new Date()) {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  const clock = { hour: "2-digit", minute: "2-digit", hour12: false };
  return {
    short: at.toLocaleString(locale, sameDay ? clock : { month: "numeric", day: "numeric", ...clock }),
    full: at.toLocaleString(locale, {
      year: "numeric",
      month: "long",
      day: "numeric",
      ...clock,
      second: "2-digit",
    }),
  };
}
