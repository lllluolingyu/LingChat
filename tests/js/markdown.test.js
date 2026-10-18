import assert from "node:assert/strict";
import { test } from "node:test";
import { renderMarkdown, createMarkdown } from "../../lingchat/shared/markdown.js";

for (const path of ["_private_/x", "a*b*c", "a**b**c", "~~archive~~", "`code`", "a?x=1&y=2"]) {
  test(`link URL survives inline transforms: ${path}`, () => {
    const url = `https://example.com/${path}`;
    const html = renderMarkdown(`[**docs**](${url})`);
    assert.ok(html.includes(`href="${url.replaceAll("&", "&amp;")}"`));
    assert.ok(html.includes("><strong>docs</strong></a>"));
  });
}

test("code spans and link labels preserve nesting without transforming hrefs", () => {
  assert.equal(renderMarkdown("**[a `b` _c_](https://example.com/_d_)**"),
    '<p><strong><a href="https://example.com/_d_" target="_blank" rel="noopener noreferrer">a <code>b</code> <em>c</em></a></strong></p>');
  assert.equal(renderMarkdown("`[docs](https://example.com)`"),
    '<p><code>[docs](https://example.com)</code></p>');
});

test("untrusted markup, schemes and quotes stay inert", () => {
  const html = renderMarkdown('<img src=x onerror=alert(1)> [x](javascript:alert) [x](https://e.test/"onmouseover="x)');
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('href="https://e.test/&quot;onmouseover=&quot;x"'));
});

test("literal placeholder-like input cannot inject or duplicate tokens", () => {
  assert.equal(renderMarkdown("\0" + "0\0 `safe`"), '<p>\0' + '0\0 <code>safe</code></p>');
});

test("nested lists, quotes, and table cells render inline content", () => {
  assert.equal(renderMarkdown("- one\n  - two\n- three"), "<ul><li>one<ul><li>two</li></ul></li><li>three</li></ul>");
  assert.equal(renderMarkdown("> **quoted**"), "<blockquote><p><strong>quoted</strong></p></blockquote>");
  const table = renderMarkdown("| Name | Link |\n| --- | --- |\n| *x* | [go](https://e.test/_x_) |");
  assert.ok(table.includes("<td><em>x</em></td>"));
  assert.ok(table.includes('href="https://e.test/_x_"'));
});

test("unclosed fences remain escaped and copy labels follow the injected language", () => {
  const html = renderMarkdown("```html\n<img onerror=x>\n**raw**");
  assert.ok(html.includes('<pre><code>&lt;img onerror=x&gt;\n**raw**</code></pre>'));
  let label = "复制";
  const localized = createMarkdown(() => label).renderMarkdown;
  assert.ok(localized("```\nx").includes("<span>复制</span>"));
  label = "Copy";
  assert.ok(localized("```\nx").includes("<span>Copy</span>"));
});
