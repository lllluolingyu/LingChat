// Shared escape-first renderer and copy controls, served by both frontends.
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

export const COPY_ICON_SVG =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';

// Hide code and complete links before emphasis can touch their markup or URLs.
// Choose a marker absent from the input, including literal control characters.
function inline(s, links = true) {
  let marker = "\x00";
  while (s.includes(marker)) marker += "\x00";
  const tokens = [];
  const pattern = links
    ? /`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g
    : /`([^`\n]+)`/g;
  s = s.replace(pattern, (_, code, label, url) => {
    const html = code !== undefined
      ? `<code>${code}</code>`
      : `<a href="${url}" target="_blank" rel="noopener noreferrer">${inline(label, false)}</a>`;
    return `${marker}${tokens.push(html) - 1}${marker}`;
  });
  s = s.replace(/\*\*([^*\n](?:[^*\n]*[^*\n])?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/(^|[^\w&])_([^_\n]+)_(?=[^\w]|$)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return s.replace(new RegExp(`${marker}(\\d+)${marker}`, "g"), (_, i) => tokens[+i]);
}


const english = (key) => ({
  "thread.copy_code": "Copy code",
  "thread.copy": "Copy",
  "thread.copied": "Copied",
})[key] || key;

export function createMarkdown(t = english) {
  function codeBlockHtml(lang, code) {
    return (
      '<div class="codeblock"><div class="codeblock-head">' +
      `<span class="codeblock-lang">${esc(lang || "text")}</span>` +
      `<button class="copy-btn" type="button" title="${esc(t("thread.copy_code"))}">` +
      COPY_ICON_SVG +
      `<span>${esc(t("thread.copy"))}</span></button></div>` +
      `<pre><code>${esc(code)}</code></pre></div>`
    );
  }

  const LIST_RE = /^(\s*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$/;
  const TABLE_SEP_RE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function buildList(items) {
    // items: [{depth, ordered, text}] — emit nested <ul>/<ol> via a tag stack.
    let html = "";
    const stack = [];
    for (const it of items) {
      while (stack.length > it.depth + 1) html += `</li></${stack.pop()}>`;
      if (stack.length === it.depth + 1) {
        html += "</li><li>";
      } else {
        while (stack.length < it.depth + 1) {
          const tag = it.ordered ? "ol" : "ul";
          html += `<${tag}><li>`;
          stack.push(tag);
        }
      }
      html += inline(esc(it.text));
    }
    while (stack.length) html += `</li></${stack.pop()}>`;
    return html;
  }

  function renderMarkdown(src) {
    const lines = src.split("\n");
    let html = "";
    let para = [];
    const flush = () => {
      if (para.length) {
        html += `<p>${para.map((l) => inline(esc(l))).join("<br>")}</p>`;
        para = [];
      }
    };

    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = line.match(/^\s*```\s*(\S*)\s*$/);
      if (fence) {
        flush();
        i++;
        const buf = [];
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++; // closing fence (or EOF while streaming — render what we have)
        html += codeBlockHtml(fence[1], buf.join("\n"));
        continue;
      }

      if (/^\s*$/.test(line)) {
        flush();
        i++;
        continue;
      }

      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flush();
        const n = h[1].length;
        html += `<h${n}>${inline(esc(h[2]))}</h${n}>`;
        i++;
        continue;
      }

      if (/^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flush();
        html += "<hr>";
        i++;
        continue;
      }

      if (/^\s*>/.test(line)) {
        flush();
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        html += `<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`;
        continue;
      }

      const li = line.match(LIST_RE);
      if (li) {
        flush();
        const items = [];
        while (i < lines.length) {
          const m = lines[i].match(LIST_RE);
          if (!m) break;
          const indent = m[1].replace(/\t/g, "  ").length;
          items.push({
            depth: Math.min(Math.floor(indent / 2), 3),
            ordered: m[3] !== undefined,
            text: m[4],
          });
          i++;
        }
        html += buildList(items);
        continue;
      }

      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
        flush();
        const splitRow = (l) =>
          l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
        const head = splitRow(line);
        i += 2;
        let table =
          "<table><thead><tr>" +
          head.map((c) => `<th>${inline(esc(c))}</th>`).join("") +
          "</tr></thead><tbody>";
        while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i])) {
          const cells = splitRow(lines[i]);
          table +=
            "<tr>" +
            head.map((_, c) => `<td>${inline(esc(cells[c] ?? ""))}</td>`).join("") +
            "</tr>";
          i++;
        }
        html += table + "</tbody></table>";
        continue;
      }

      para.push(line);
      i++;
    }
    flush();
    return html;
  }

  // --- clipboard -------------------------------------------------------------

  async function copyText(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* nothing left to try */ }
      ta.remove();
    }
    const label = btn.querySelector("span");
    btn.classList.add("copied");
    if (label) label.textContent = t("thread.copied");
    setTimeout(() => {
      btn.classList.remove("copied");
      if (label) label.textContent = t("thread.copy");
    }, 1400);
  }

  // One delegated listener serves every copy button the renderer (or the thread
  // view's message actions) ever creates under `root`.
  function attachCopyHandler(root) {
    root.addEventListener("click", (e) => {
      const btn = e.target.closest(".copy-btn");
      if (!btn) return;
      if (btn.dataset.raw !== undefined) {
        copyText(btn.dataset.raw, btn);
        return;
      }
      const block = btn.closest(".codeblock");
      const code = block && block.querySelector("pre code");
      if (code) copyText(code.textContent, btn);
    });
  }
  return { renderMarkdown, attachCopyHandler };
}

export const { renderMarkdown, attachCopyHandler } = createMarkdown();

// --- streaming -------------------------------------------------------------
//
// Re-rendering a whole streamed reply every frame is quadratic over its
// length. In renderMarkdown a blank line outside a code fence ends every block
// (paragraph, list, table, quote), so render(a + b) === render(a) + render(b)
// whenever `a` stops at such a line. The stream hands that settled prefix out
// once as `commit`; only the `tail` after it is rendered again on later
// updates. Concatenating every commit and the latest tail always equals
// render(raw).
//
// The scan mirrors the renderer's fence rules (same regexes). One case can
// disagree: a fence-like line that has a `|` may be a table row to the
// renderer. When the scan sees one, it stops committing for the rest of the
// reply and just renders the whole tail, which is still correct.
const FENCE_OPEN_RE = /^\s*```\s*(\S*)\s*$/;
const FENCE_CLOSE_RE = /^\s*```\s*$/;
const BLANK_RE = /^\s*$/;

export function createMarkdownStream(render = renderMarkdown) {
  let committed = 0; // raw offset already handed out as commits
  let scanned = 0; // raw offset of the first line not yet scanned
  let boundary = 0; // start of the latest blank line outside a fence
  let inFence = false;
  let ambiguous = false;

  return {
    // `raw` must extend the text from the previous call (append-only).
    update(raw) {
      if (raw.length < scanned) throw new Error("markdown stream text must only grow");
      // Only complete lines are scanned: a partial last line may still turn
      // into a fence or a blank line.
      let nl;
      while ((nl = raw.indexOf("\n", scanned)) !== -1) {
        const line = raw.slice(scanned, nl);
        if (inFence) {
          if (FENCE_CLOSE_RE.test(line)) inFence = false;
        } else if (FENCE_OPEN_RE.test(line)) {
          inFence = true;
          if (line.includes("|")) ambiguous = true;
        } else if (BLANK_RE.test(line)) {
          boundary = scanned;
        }
        scanned = nl + 1;
      }
      let commit = "";
      if (!ambiguous && boundary > committed) {
        commit = render(raw.slice(committed, boundary));
        committed = boundary;
      }
      return { commit, tail: render(raw.slice(committed)) };
    },
  };
}
