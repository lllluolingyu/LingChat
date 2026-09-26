# Derived from LingChat; Copyright LingChat contributors; Apache-2.0.
"""Validated attachments shared by the browser bridge and all adapters."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote

from lingcore.media import attachment_from_wire
from lingcore.media_types import (
    MAX_ATTACHMENTS,
    TOTAL_ATTACHMENT_MAX_BYTES,
    decoded_payload_size,
    sanitize_name,
)
from lingcore.message import Attachment


def attachment_payloads(
    attachments: list[Attachment], *, downloadable: bool = False
) -> list[dict[str, Any]]:
    """Return safe browser metadata for attachments.

    ``fallback_text`` is always model-only. Images retain their validated base64
    bytes for inline previews; every other kind omits ``data`` and is fetched as
    an authenticated download only after it has a stored message sequence.
    """
    payloads: list[dict[str, Any]] = []
    for attachment in attachments:
        excluded = {"fallback_text"}
        if attachment.kind != "image":
            excluded.add("data")
        payload = attachment.model_dump(exclude=excluded)
        payload["size"] = decoded_payload_size(attachment.data)
        payload["download"] = downloadable
        payloads.append(payload)
    return payloads


def validate_attachments(raw: object) -> list[Attachment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments must be a list")
    if len(raw) > MAX_ATTACHMENTS:
        raise ValueError(f"too many attachments ({len(raw)}; limit {MAX_ATTACHMENTS})")
    out: list[Attachment] = []
    for item in raw:
        try:
            out.append(attachment_from_wire(item))
        except Exception as e:
            raise ValueError(str(e)) from None
    if sum(decoded_payload_size(a.data) for a in out) > TOTAL_ATTACHMENT_MAX_BYTES:
        raise ValueError("attachments exceed the total byte limit")
    return out


def content_disposition(name: str | None) -> str:
    safe = sanitize_name(name, fallback="attachment")
    fallback = safe.encode("ascii", errors="replace").decode("ascii")
    fallback = fallback.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(safe, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def display_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep non-image bytes in the authenticated download route only."""
    result = copy.deepcopy(turns)
    for turn in result:
        for msg in turn["frames"]:
            for item in msg.get("attachments", []):
                item.pop("fallback_text", None)
                data = item.get("data", "")
                item["size"] = decoded_payload_size(data) if data else 0
                item["download"] = bool(data)
                if item.get("kind") != "image":
                    item.pop("data", None)
    return result
