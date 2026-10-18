import assert from "node:assert/strict";
import { test } from "node:test";
import { createNotes } from "../../lingchat/shared/notes.js";

const { todoCounts, compactNote, pluginActionText, isRedundantPluginNote } = createNotes();

test("todo counts tolerate absent and partially populated checklists", () => {
  assert.deepEqual(todoCounts(null), { done: 0, total: 0 });
  assert.deepEqual(todoCounts([null, { status: "completed" }, { status: "pending" }]), { done: 1, total: 3 });
});

test("compaction counts round, clamp, and distinguish unknown estimates", () => {
  assert.equal(compactNote({ summarized_messages: 1.2, before_tokens: 120, after_tokens: 20 }),
    "Context compacted: summarized 1 earlier message (~120 → ~20 tokens).");
  assert.match(compactNote({ summarized_messages: -3, before_tokens: -5 }), /0 earlier messages \(~0 → ~\? tokens\)/);
  assert.match(compactNote({}), /some earlier messages/);
});

test("plugin actions translate known hooks and preserve unknown actions", () => {
  assert.equal(pluginActionText({ hook: "before_tool", action: "asked" }), "asked for approval");
  assert.equal(pluginActionText({ hook: "new_hook", action: "failed" }), "hook failed");
  assert.equal(pluginActionText({ hook: "new_hook", action: "allowed" }), "new_hook · allowed");
  assert.equal(isRedundantPluginNote({ hook: "user_message", action: "blocked" }), true);
  assert.equal(isRedundantPluginNote({ hook: "before_tool", action: "denied" }), false);
  assert.equal(createNotes({ t: key => `translated:${key}` }).pluginActionText({ action: "failed" }), "translated:plugin.failed");
});
