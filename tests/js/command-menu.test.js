import assert from "node:assert/strict";
import { test } from "node:test";
import { createCommandMenu } from "../../lingchat/shared/command-menu.js";

// Only the DOM surface used by the menu. Browser smoke checks cover real focus,
// event propagation and layout; these tests exercise the shared controller.
class Element {
  children = [];
  attributes = {};
  listeners = {};
  value = "";
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  prepend(child) { this.children.unshift(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, event = {}) { for (const fn of this.listeners[type] || []) fn(event); }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(selector === `.${child.className}` ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  focus() { this.focused = true; }
  setSelectionRange(start, end) { this.selection = [start, end]; }
  scrollIntoView() {}
}

function setup() {
  globalThis.document = { createElement: () => new Element() };
  const input = new Element(), inner = new Element();
  let changes = 0;
  const controller = createCommandMenu({ inputEl: input, formEl: { querySelector: () => inner }, onChange() { changes++; } });
  controller.setCommands([{ name: "/plan", description: "Plan" }, { name: "/plugin" }]);
  const type = value => { input.value = value; input.emit("input"); };
  const key = (value, options = {}) => {
    const event = { key: value, prevented: false, preventDefault() { this.prevented = true; }, ...options };
    const handled = controller.handleKeydown(event);
    return { handled, prevented: event.prevented };
  };
  return { input, menu: inner.children[0], controller, type, key, changes: () => changes };
}

test("filters bare prefixes case-insensitively; arguments close the menu", () => {
  const { input, menu, type } = setup();
  type("/PL");
  assert.equal(menu.hidden, false);
  assert.equal(menu.querySelectorAll(".command-option").length, 2);
  assert.equal(input.attributes["aria-expanded"], "true");
  for (const value of ["/plan arg", " /plan", "plain", "/plan\n"]) {
    type(value);
    assert.equal(menu.hidden, true);
    assert.equal(input.attributes["aria-activedescendant"], undefined);
  }
});

test("arrows wrap, Tab inserts the selection, and focus stays in the input", () => {
  const { input, menu, type, key, changes } = setup();
  type("/p");
  assert.deepEqual(key("ArrowUp"), { handled: true, prevented: true });
  assert.equal(input.attributes["aria-activedescendant"], "command-option-1");
  key("ArrowDown");
  assert.equal(input.attributes["aria-activedescendant"], "command-option-0");
  key("ArrowDown");
  key("Tab");
  assert.equal(input.value, "/plugin ");
  assert.equal(input.focused, true);
  assert.equal(menu.hidden, true);
  assert.equal(changes(), 1);
});

test("exact Enter reaches send; partial Enter inserts; IME and Shift+Enter pass through", () => {
  const { input, type, key } = setup();
  type("/plan");
  assert.deepEqual(key("Enter"), { handled: false, prevented: false });
  assert.equal(input.value, "/plan");
  type("/p");
  assert.deepEqual(key("Enter", { isComposing: true }), { handled: false, prevented: false });
  assert.deepEqual(key("Enter", { shiftKey: true }), { handled: false, prevented: false });
  assert.equal(input.value, "/p");
  assert.deepEqual(key("Enter"), { handled: true, prevented: true });
  assert.equal(input.value, "/plan ");
});

test("Escape dismissal resets on new hello metadata or new input", () => {
  const { menu, type, key, controller } = setup();
  type("/p");
  key("Escape");
  assert.equal(menu.hidden, true);
  controller.update();
  assert.equal(menu.hidden, true);
  controller.setCommands([{ name: "/plan" }]);
  assert.equal(menu.hidden, false);
  key("Escape");
  type("/pl");
  assert.equal(menu.hidden, false);
});

test("suggestions are capped, malformed metadata is ignored, blur removes active descendant", () => {
  const { input, menu, type, controller } = setup();
  controller.setCommands([null, {}, ...Array.from({ length: 30 }, (_, i) => ({ name: `/p${i}` }))]);
  type("/p");
  assert.equal(menu.querySelectorAll(".command-option").length, 12);
  input.emit("blur");
  assert.equal(menu.hidden, true);
  assert.equal(input.attributes["aria-activedescendant"], undefined);
});
