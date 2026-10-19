import assert from "node:assert/strict";
import { test } from "node:test";
import {
  cacheHitPct,
  compactTokens,
  createLedger,
  replyTime,
  tokenCounts,
} from "../../lingchat/shared/usage.js";

test("cache counts join the prompt only when they sit beside input", () => {
  // Codex / LingCore: cached is part of input.
  assert.deepEqual(tokenCounts({ input: 100, cached: 60, output: 20, cache_in_input: true }),
    { prompt: 100, cached: 60, output: 20, total: 120 });
  // Claude: cache reads and writes are extra prompt tokens.
  assert.deepEqual(
    tokenCounts({ input: 12, cached: 4, cache_write: 6, output: 3, cache_in_input: false }),
    { prompt: 22, cached: 4, output: 3, total: 25 });
  // A frame stored before the flag: only a separate count can exceed input.
  assert.equal(tokenCounts({ input: 2, cached: 40, output: 1 }).prompt, 42);
  assert.equal(tokenCounts({ input: 50, cached: 40, output: 1 }).prompt, 50);
  assert.deepEqual(tokenCounts({ input: -5, output: "x" }), { prompt: 0, cached: 0, output: 0, total: 0 });
});

test("per-request and per-turn usage adds up within a reply and across the session", () => {
  const ledger = createLedger();
  ledger.begin();
  ledger.usage({ scope: "request", input: 100, cached: 80, output: 5, cache_in_input: true });
  ledger.step();
  ledger.usage({ scope: "request", input: 120, cached: 100, output: 7, cache_in_input: true });
  const first = ledger.current();
  ledger.timing({ ended_at: "2026-10-19T07:32:00Z", model_ms: 2000 });
  assert.equal(ledger.begin(), first);
  ledger.usage({ scope: "request", input: 30, output: 3, cache_in_input: true });
  assert.deepEqual(first.tokens, { prompt: 220, cached: 180, output: 12, total: 232 });
  const s = ledger.summary();
  assert.equal(s.turns, 2);
  assert.equal(s.steps, 1);
  assert.equal(s.tokens.total, 265);
  // Only the timed reply contributes to throughput.
  assert.equal(s.rate, 6);
  assert.equal(Math.floor(cacheHitPct(s.tokens)), 72);
});

test("running conversation totals are diffed per reply", () => {
  const ledger = createLedger();
  ledger.begin();
  ledger.usage({ scope: "conversation", input: 50, cached: 10, output: 5, context_pct: 4 });
  ledger.usage({ scope: "conversation", input: 100, cached: 60, output: 20, context_pct: 12 });
  const first = ledger.current();
  ledger.begin();
  ledger.usage({ scope: "conversation", input: 180, cached: 140, output: 30, context_pct: 19 });
  const second = ledger.current();
  assert.equal(first.tokens.total, 120);
  assert.deepEqual(second.tokens, { prompt: 80, cached: 80, output: 10, total: 90 });
  assert.equal(ledger.summary().tokens.total, 210);
  assert.equal(ledger.summary().contextPct, 19);
  // A total that went backwards restarted: all of it is new.
  ledger.begin();
  ledger.usage({ scope: "conversation", input: 40, output: 2 });
  assert.equal(ledger.current().tokens.total, 42);
});

test("a session with no usage reports no rate, hit rate, context, or cost", () => {
  const ledger = createLedger();
  ledger.begin();
  ledger.timing({ ended_at: "2026-10-19T07:32:00Z", model_ms: 900 });
  const s = ledger.summary();
  assert.equal(s.metered, false);
  assert.equal(s.rate, null);
  assert.equal(cacheHitPct(s.tokens), null);
  assert.equal(s.contextPct, null);
  assert.equal(s.costUsd, null);
  ledger.usage({ scope: "turn", input: 1, output: 1, cost_usd: 0.02, context_pct: 140 });
  assert.equal(ledger.summary().costUsd, 0.02);
  assert.equal(ledger.summary().contextPct, 100);
});

test("token counts and reply times stay compact", () => {
  assert.equal(compactTokens(950), "950");
  assert.equal(compactTokens(12_345), "12.3K");
  assert.equal(compactTokens(14_200_000), "14.2M");
  const now = new Date(2026, 9, 19, 18, 0);
  const today = replyTime(new Date(2026, 9, 19, 15, 32).toISOString(), "en", now);
  assert.equal(today.short, "15:32");
  const earlier = replyTime(new Date(2026, 9, 17, 9, 5).toISOString(), "en", now);
  assert.match(earlier.short, /10\/17.*09:05/);
  assert.equal(replyTime("not a date", "en", now), null);
});
