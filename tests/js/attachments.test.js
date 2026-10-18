import assert from "node:assert/strict";
import { test } from "node:test";
import { createAttachmentQueue, mediaTypeForFile } from "../../lingchat/shared/attachments.js";

const limits = { max_attachments: 1, total_max_bytes: 8, file_max_bytes: 8, image_max_bytes: 8 };
const file = (name = "notes.txt", size = 4, type = "") => ({ name, size, type });
const dataUrl = "data:text/plain;base64,dGVzdA==";
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function setup(options = {}) {
  const errors = [];
  const queue = createAttachmentQueue({ getLimits: () => limits, onChange() {}, onError: error => errors.push(error), ...options });
  return { queue, errors };
}

test("extension MIME fallback also drives capability checks", async () => {
  assert.equal(mediaTypeForFile(file("IMAGE.PNG")), "image/png");
  const { queue, errors } = setup({ getCapabilities: () => ({ images: true, files: false }), read: async () => dataUrl });
  await queue.add([file("IMAGE.PNG")]);
  assert.equal(queue.items[0].media_type, "image/png");
  assert.deepEqual(errors, []);
});

test("an extension-only image cannot bypass the no-images capability", async () => {
  const { queue, errors } = setup({ getCapabilities: () => ({ images: false, files: true }), read: async () => dataUrl });
  await queue.add([file("image.png")]);
  assert.equal(queue.items.length, 0);
  assert.match(errors[0], /does not support image/);
});

for (const limit of ["count", "total"]) {
  test(`overlapping pastes enforce the ${limit} limit after the first read`, async () => {
    const read = deferred();
    let reads = 0;
    const { queue, errors } = setup({
      getLimits: () => ({ ...limits, max_attachments: limit === "count" ? 1 : 8, total_max_bytes: 7 }),
      read: () => { reads++; return read.promise; },
    });
    const first = queue.add([file()]);
    const second = queue.add([file()]);
    assert.equal(queue.busy, true);
    await Promise.resolve();
    assert.equal(reads, 1);
    read.resolve(dataUrl);
    await Promise.all([first, second]);
    assert.equal(reads, 1);
    assert.equal(queue.items.length, 1);
    assert.equal(errors.length, 1);
    assert.equal(queue.busy, false);
  });
}

test("session reset discards stale reads without unlocking a new batch", async () => {
  const old = deferred(), fresh = deferred();
  let reads = 0;
  const { queue, errors } = setup({ read: () => (++reads === 1 ? old : fresh).promise });
  const first = queue.add([file("old.txt")]);
  await Promise.resolve();
  queue.reset();
  const second = queue.add([file("new.txt")]);
  old.resolve(dataUrl);
  await first;
  assert.equal(queue.busy, true);
  assert.deepEqual(queue.items, []);
  fresh.resolve(dataUrl);
  await second;
  assert.deepEqual(queue.items.map(item => item.name), ["new.txt"]);
  assert.deepEqual(errors, []);
});

test("read and size errors are localized and always release the busy state", async () => {
  const { queue, errors } = setup({ t: key => key, read: async () => { throw new Error("OS error"); } });
  await queue.add([file()]);
  assert.equal(queue.busy, false);
  assert.deepEqual(errors, ["composer.read_failed"]);
  const oversized = setup({ getLimits: () => ({ ...limits, file_max_bytes: 2 }), t: key => key });
  await oversized.queue.add([file()]);
  assert.deepEqual(oversized.errors, ["composer.file_too_large"]);
});
