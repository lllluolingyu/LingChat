/* Adapted from LingChat; Copyright LingChat contributors; Apache-2.0. */
import { createMarkdown } from "/shared/markdown.js";
import { t } from "./i18n.js";

export { COPY_ICON_SVG, esc } from "/shared/markdown.js";
export const { renderMarkdown, attachCopyHandler } = createMarkdown(t);
