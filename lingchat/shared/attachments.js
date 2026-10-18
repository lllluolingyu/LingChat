// Shared MIME inference and serialized attachment reads. Each queue belongs to
// one composer; reset invalidates queued and in-flight reads on session changes.
export function mediaTypeForFile(file) {
  if (file.type) return file.type;
  const name = file.name.toLowerCase();
  const extensionTypes = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".html": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".xml": "application/xml",
    ".js": "application/javascript",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".sh": "application/x-sh",
  };
  for (const [extension, mediaType] of Object.entries(extensionTypes)) {
    if (name.endsWith(extension)) return mediaType;
  }
  return "";
}

const TEXTUAL_APPLICATION_TYPES = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "application/ecmascript",
  "application/yaml",
  "application/x-yaml",
  "application/toml",
  "application/x-sh",
  "application/x-shellscript",
]);

// Cosmetic only: LingCore inspects the decoded bytes and may reclassify this.
export function displayKind(mediaType) {
  if (mediaType.startsWith("image/")) return "image";
  if (mediaType === "application/pdf") return "file";
  if (mediaType.startsWith("text/") || TEXTUAL_APPLICATION_TYPES.has(mediaType)) {
    return "text";
  }
  return "binary";
}


const english = (key, values = {}) => ({
  "composer.too_many": `You can attach at most ${values.count} files per message.`,
  "composer.too_large": `Attachments exceed the ${values.mb}MB total limit per message.`,
  "composer.file_too_large": `${values.name} is too large (${values.bytes} bytes)`,
  "composer.read_failed": `Failed to read ${values.name}`,
  "composer.images_only": "This backend supports image attachments only.",
  "composer.no_images": "This backend does not support image attachments.",
})[key] || key;

export function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reader.onabort = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export function createAttachmentQueue({
  getLimits,
  getCapabilities = () => ({ images: true, files: true }),
  onChange,
  onError,
  t = english,
  read = readDataUrl,
}) {
  let items = [];
  let generation = 0;
  let pending = 0;
  let tail = Promise.resolve();

  function totalBytes() {
    return items.reduce((total, attachment) => {
      const data = attachment.data || "";
      const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
      return total + Math.floor((data.length * 3) / 4) - padding;
    }, 0);
  }

  async function addBatch(files, gen) {
    for (const file of files) {
      if (gen !== generation) return;
      const limits = getLimits();
      if (items.length >= limits.max_attachments) {
        onError(t("composer.too_many", { count: limits.max_attachments }));
        break;
      }
      if (totalBytes() + file.size > limits.total_max_bytes) {
        onError(t("composer.too_large", { mb: Math.floor(limits.total_max_bytes / (1024 * 1024)) }));
        break;
      }
      try {
        const mediaType = mediaTypeForFile(file);
        const isImage = mediaType.startsWith("image/");
        const capabilities = getCapabilities();
        if (!isImage && !capabilities.files) throw new Error(t("composer.images_only"));
        if (isImage && !capabilities.images) throw new Error(t("composer.no_images"));
        const limit = isImage ? limits.image_max_bytes : limits.file_max_bytes;
        if (file.size > limit) {
          throw new Error(t("composer.file_too_large", { name: file.name, bytes: file.size }));
        }
        let dataUrl;
        try { dataUrl = await read(file); }
        catch { throw new Error(t("composer.read_failed", { name: file.name })); }
        if (gen !== generation) return;
        items.push({
          kind: displayKind(mediaType),
          media_type: mediaType,
          data: dataUrl.split(",", 2)[1] || "",
          name: file.name || "attachment",
        });
        onChange();
      } catch (error) {
        if (gen !== generation) return;
        onError(error.message || String(error));
      }
    }
  }

  return {
    get items() { return items; },
    get busy() { return pending > 0; },
    add(files) {
      const batch = Array.from(files); // FileList can change when its input is reset.
      if (!batch.length) return tail;
      const gen = generation;
      pending++;
      onChange(); // Disable Send synchronously, before the first read yields.
      const task = tail.then(() => addBatch(batch, gen));
      tail = task.finally(() => {
        if (gen !== generation) return;
        pending--;
        onChange();
      });
      return tail;
    },
    remove(index) {
      items.splice(index, 1);
      onChange();
    },
    reset() {
      generation++;
      items = [];
      pending = 0;
      tail = Promise.resolve();
      onChange();
    },
  };
}
