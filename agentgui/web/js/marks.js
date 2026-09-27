/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
// Backend marks: the glyph that identifies which agent a row, badge or picker
// entry belongs to.
//
// Every mark is drawn in `currentColor` with no intrinsic size, like the rest
// of this UI's icons, so one glyph works on the clay avatar disc, in a small
// hairline badge and beside a <select> without a second copy per context.
//
// The GPT mark is an original glyph in the shape family of OpenAI's blossom, not
// a copy of their asset, and it identifies rather than brands — the same way the
// radiating asterisk says "Claude". Trademarks belong to their owners.

// A radiating asterisk. Also the fallback: it reads as "an agent" rather than
// as any particular vendor.
export const AGENT_MARK =
  '<svg viewBox="0 0 32 32" aria-hidden="true">' +
  '<path d="M16.0 13.8L16.0 5.0M17.6 14.4L22.1 9.9M18.2 16.0L27.0 16.0M17.6 17.6L22.1 22.1' +
  'M16.0 18.2L16.0 27.0M14.4 17.6L9.9 22.1M13.8 16.0L5.0 16.0M14.4 14.4L9.9 9.9" ' +
  'fill="none" stroke="currentColor" stroke-width="3.1" stroke-linecap="round"/></svg>';

// The OpenAI blossom: six lobes around a hexagonal centre, built as three
// identical stadium loops rotated 60° about the middle rather than as one
// traced outline. Constructed geometrically on purpose — the coordinates follow
// from (half-length 11.6, half-width 4.4) and stay legible at the 15px the
// avatar renders, where a faithful trace of the wordmark's knot turns to mush.
// Its 3.0 stroke matches AGENT_MARK's, so neither mark outweighs the other.
const GPT_LOBE =
  "M8.8 11.6 L23.2 11.6 A4.4 4.4 0 0 1 23.2 20.4 " +
  "L8.8 20.4 A4.4 4.4 0 0 1 8.8 11.6 Z";

export const GPT_MARK =
  '<svg viewBox="0 0 32 32" aria-hidden="true">' +
  [0, 60, 120]
    .map(
      (deg) =>
        `<path d="${GPT_LOBE}" transform="rotate(${deg} 16 16)" fill="none" ` +
        'stroke="currentColor" stroke-width="3" stroke-linejoin="round"/>',
    )
    .join("") +
  "</svg>";

/**
 * True when a catalog entry is a GPT-series model.
 *
 * The Codex backend is OpenAI's CLI, so every model it serves is one. A
 * LingCore profile can also front an OpenAI model, and there the only signal is
 * the operator's own id/native name — hence the word match as well.
 */
export function isGpt(model) {
  if (!model) return false;
  if (model.backend === "codex") return true;
  const name = `${model.id || ""} ${model.native_model || ""}`;
  return /\bgpt|\bo[34]\b|chatgpt/i.test(name);
}

/** The mark for a catalog entry, or for a bare backend name from `hello`. */
export function markFor(model) {
  const entry = typeof model === "string" ? { backend: model, id: model } : model;
  return isGpt(entry) ? GPT_MARK : AGENT_MARK;
}
