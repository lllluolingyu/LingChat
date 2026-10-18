// Paints a growing markdown reply without rebuilding what has settled.
//
// Settled blocks from createMarkdownStream are appended to `body` once and then
// left alone. Each paint replaces only the nodes for the unfinished tail. That
// keeps the per-frame cost near the tail's size instead of the whole reply.
// Earlier paragraphs also stay put, so a selection in them (or a click on a
// finished code block's copy button) survives the stream. There is no wrapper
// element, so `.md > :first-child` / `:last-child` still match.
import { createMarkdownStream } from "./markdown.js";

export function createStreamView(body, render) {
  let stream = createMarkdownStream(render);
  let frozen = 0; // leading body.childNodes owned by committed blocks
  let painted = "";

  function paint(raw) {
    const { commit, tail } = stream.update(raw);
    while (body.childNodes.length > frozen) body.lastChild.remove();
    if (commit) {
      body.insertAdjacentHTML("beforeend", commit);
      frozen = body.childNodes.length;
    }
    body.insertAdjacentHTML("beforeend", tail);
    painted = raw;
  }

  return {
    paint,
    // The authoritative final text normally equals what streamed in, which
    // only needs one more tail paint. If it differs (the server rewrote it),
    // rebuild from scratch.
    finish(raw) {
      if (raw.startsWith(painted)) {
        paint(raw);
        return;
      }
      stream = createMarkdownStream(render);
      frozen = 0;
      body.textContent = "";
      paint(raw);
    },
  };
}
