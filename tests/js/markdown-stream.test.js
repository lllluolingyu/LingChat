import assert from "node:assert/strict";
import { test } from "node:test";
import { createMarkdownStream, renderMarkdown } from "../../lingchat/shared/markdown.js";
import { createStreamView } from "../../lingchat/shared/stream-view.js";

const SAMPLES = {
  paragraphs: "Intro line\nsecond line\n\nNext **para** with [a link](https://e.test/_x_)\n\n\nLast",
  lists: "- one\n  - two\n- three\n\n1. first\n2. second\n\nafter",
  table: "| A | B |\n| --- | --- |\n| 1 | *2* |\n| 3 | 4 |\n\ntext after table",
  quote: "> quoted\n> more\n\n> second quote\n\nplain",
  fenceWithBlanks: "Before\n\n```js\nconst a = 1;\n\n\nconst b = 2;\n```\n\nAfter\n\n```\nunclosed\n\nstill code",
  fenceInList: "- item\n```\n- not a list\n\n- still code\n```\n\ndone",
  headingsAndRules: "# Title\n\ntext\n\n---\n\n## Sub\n\n***\n\nend",
  crlf: "line one\r\n\r\nline two\r\n```\r\ncode\r\n\r\nmore\r\n```\r\n\r\ntail",
  // A fence-looking table row: the renderer keeps it in the table, so the
  // stream must not treat the later ``` as closing a fence.
  ambiguousTableRow: "| a | b |\n| --- | --- |\n```x|y\n\nmiddle\n\n```\ninside?\n\nreal end",
  inlineCode: "Use `a\n\nb` carefully\n\n`ok` then *em*",
};

function* chunkings(text) {
  yield [text];
  yield Array.from(text);
  let seed = 7;
  const rand = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  for (let run = 0; run < 25; run++) {
    const parts = [];
    for (let i = 0; i < text.length;) {
      const n = 1 + Math.floor(rand() * 12);
      parts.push(text.slice(i, i + n));
      i += n;
    }
    yield parts;
  }
}

for (const [name, text] of Object.entries(SAMPLES)) {
  test(`stream commits + tail equal a full render at every step: ${name}`, () => {
    for (const parts of chunkings(text)) {
      const stream = createMarkdownStream();
      let raw = "";
      let committed = "";
      for (const part of parts) {
        raw += part;
        const { commit, tail } = stream.update(raw);
        committed += commit;
        assert.equal(committed + tail, renderMarkdown(raw));
      }
    }
  });
}

test("settled blocks are committed once, not re-rendered", () => {
  let calls = [];
  const render = (src) => {
    calls.push(src);
    return renderMarkdown(src);
  };
  const stream = createMarkdownStream(render);
  stream.update("first para\n\nsecond");
  calls = [];
  stream.update("first para\n\nsecond para grows");
  assert.deepEqual(calls, ["\nsecond para grows"]);
});

test("nothing inside an open fence is committed", () => {
  const stream = createMarkdownStream();
  assert.equal(stream.update("```\na\n\nb\n\n").commit, "");
  assert.notEqual(stream.update("```\na\n\nb\n\n```\n\nafter").commit, "");
});

test("a shrinking text is rejected", () => {
  const stream = createMarkdownStream();
  stream.update("abc\n\ndef\n");
  assert.throws(() => stream.update("abc"));
});

// One node per insertAdjacentHTML call is enough to check which nodes the
// view keeps frozen and which it replaces.
function fakeBody() {
  const body = {
    childNodes: [],
    get lastChild() {
      const node = body.childNodes.at(-1);
      return node && { remove: () => body.childNodes.pop() };
    },
    insertAdjacentHTML(position, html) {
      assert.equal(position, "beforeend");
      if (html) body.childNodes.push(html);
    },
    set textContent(value) {
      assert.equal(value, "");
      body.childNodes = [];
    },
    get html() {
      return body.childNodes.join("");
    },
  };
  return body;
}

test("stream view keeps committed nodes and swaps only the tail", () => {
  const body = fakeBody();
  const view = createStreamView(body, renderMarkdown);
  view.paint("one\n\ntw");
  const frozenNode = body.childNodes[0];
  view.paint("one\n\ntwo\n\nthr");
  assert.equal(body.childNodes[0], frozenNode);
  assert.equal(body.html, renderMarkdown("one\n\ntwo\n\nthr"));
  view.finish("one\n\ntwo\n\nthree");
  assert.equal(body.html, renderMarkdown("one\n\ntwo\n\nthree"));
});

test("stream view rebuilds when the final text rewrites the streamed text", () => {
  const body = fakeBody();
  const view = createStreamView(body, renderMarkdown);
  view.paint("draft para\n\nmore");
  view.finish("rewritten\n\nanswer");
  assert.equal(body.html, renderMarkdown("rewritten\n\nanswer"));
});
