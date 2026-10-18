// Snapshot immediately before replay, after the fetch has finished, so user
// scrolling/toggling while a request is in flight is also preserved.
function detailKeys(thread) {
  const occurrences = new Map();
  return Array.from(thread.querySelectorAll("details"), (detail, index) => {
    const base = detail.dataset.viewKey || `details:${index}`;
    const n = occurrences.get(base) || 0;
    occurrences.set(base, n + 1);
    return [detail, `${base}:${n}`];
  });
}

export function captureView(thread, scroller, stick) {
  const top = scroller.getBoundingClientRect().top;
  const rows = Array.from(thread.children);
  const index = rows.findIndex(row => row.getBoundingClientRect().bottom > top);
  const anchor = rows[index];
  return {
    stick,
    scrollTop: scroller.scrollTop,
    details: new Map(detailKeys(thread).map(([detail, key]) => [key, detail.open])),
    anchor: anchor && {
      index,
      key: anchor.dataset.viewKey,
      offset: anchor.getBoundingClientRect().top - top,
    },
  };
}

export function restoreView(thread, scroller, view) {
  for (const [detail, key] of detailKeys(thread)) {
    if (view.details.has(key)) detail.open = view.details.get(key);
  }
  if (view.stick) {
    scroller.scrollTop = scroller.scrollHeight;
    return;
  }
  scroller.scrollTop = view.scrollTop;
  const anchor = view.anchor;
  if (!anchor) return;
  const rows = Array.from(thread.children);
  const row = (anchor.key && rows.find(row => row.dataset.viewKey === anchor.key)) || rows[anchor.index];
  if (row) {
    scroller.scrollTop += row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchor.offset;
  }
}
