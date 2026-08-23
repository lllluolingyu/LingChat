"""Attachment parity and authenticated stored-byte downloads."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, AsyncIterator

from lingcore.llm import LLMChunk
from lingcore.media_types import (
    FILE_MAX_BYTES,
    IMAGE_MAX_BYTES,
    MAX_ATTACHMENTS,
    TOTAL_ATTACHMENT_MAX_BYTES,
)
from lingcore.message import Message
from starlette.testclient import TestClient

from lingchat.server import create_app


class FakeLLM:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.calls: list[list[Message]] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        turn = self._turns.pop(0) if self._turns else {}
        text = str(turn.get("text", ""))
        if text:
            yield LLMChunk(text_delta=text)
        yield LLMChunk(tool_calls=None, finish_reason="stop")


def _write_profile(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "config.yaml"
    profile.write_text(
        f"name: attachments\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n",
        encoding="utf-8",
    )
    return profile


def _wire(name: str, data: bytes, media_type: str = "") -> dict[str, str]:
    return {
        "name": name,
        "media_type": media_type,
        "data": base64.b64encode(data).decode("ascii"),
    }


def _drain_until(ws, stop_type: str) -> list[dict]:
    messages = []
    while True:
        message = ws.receive_json()
        messages.append(message)
        if message["type"] == stop_type:
            return messages


def _user_message(call: list[Message]) -> Message:
    return next(
        message for message in call if message.role == "user" and not message.name
    )


def test_core_attachment_limit_and_hello_limits(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM([{"text": "accepted"}, {"text": "still alive"}])
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    payloads = [
        _wire(f"note-{index}.txt", b"hello") for index in range(MAX_ATTACHMENTS)
    ]

    with TestClient(app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["limits"] == {
            "max_attachments": MAX_ATTACHMENTS,
            "image_max_bytes": IMAGE_MAX_BYTES,
            "file_max_bytes": FILE_MAX_BYTES,
            "total_max_bytes": TOTAL_ATTACHMENT_MAX_BYTES,
        }

        ws.send_json({"type": "user", "text": "eight", "attachments": payloads})
        accepted = _drain_until(ws, "turn_end")
        assert any(frame["type"] == "final" for frame in accepted)
        assert len(_user_message(fake.calls[0]).attachments) == MAX_ATTACHMENTS

        ws.send_json(
            {
                "type": "user",
                "text": "nine",
                "attachments": payloads + [_wire("extra.txt", b"extra")],
            }
        )
        error = ws.receive_json()
        assert error["type"] == "error"
        assert f"limit {MAX_ATTACHMENTS}" in error["message"]
        assert ws.receive_json()["type"] == "turn_end"

        ws.send_json({"type": "user", "text": "after the error"})
        survived = _drain_until(ws, "turn_end")
        assert any(
            frame["type"] == "final" and frame["text"] == "still alive"
            for frame in survived
        )


def test_text_and_binary_are_classified_ingested_and_prepared(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM([{"text": "seen"}])
    app = create_app(profile, require_auth=False, llm_factory=lambda: fake)
    attachments = [
        _wire("notes.txt", b"plain text", "text/plain"),
        _wire("blob.bin", b"a\x00b", "application/octet-stream"),
    ]

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "inspect", "attachments": attachments})
        _drain_until(ws, "turn_end")

    user = _user_message(fake.calls[0])
    assert [attachment.kind for attachment in user.attachments] == ["text", "binary"]
    assert user.attachments[0].fallback_text == "plain text"
    assert "binary file saved to attachments/blob.bin" in (
        user.attachments[1].fallback_text or ""
    )
    assert (
        tmp_path / "workspace" / "attachments" / "notes.txt"
    ).read_bytes() == b"plain text"
    assert (
        tmp_path / "workspace" / "attachments" / "blob.bin"
    ).read_bytes() == b"a\x00b"


def test_transcript_trims_non_images_and_downloads_exact_bytes(tmp_path):
    profile = _write_profile(tmp_path)
    fake = FakeLLM([{"text": "stored"}])
    app = create_app(profile, auth_token="secret", llm_factory=lambda: fake)
    raw = {
        "image": b"\x89PNG\r\n\x1a\npreview",
        "file": b"%PDF-1.7\nbody",
        "text": b"<script>not executable</script>",
        "binary": b"a\x00b",
    }
    attachments = [
        _wire("preview.png", raw["image"], "image/png"),
        _wire("paper.pdf", raw["file"], "application/pdf"),
        _wire("notes.html", raw["text"], "text/html"),
        _wire("blob.bin", raw["binary"], "application/octet-stream"),
    ]
    headers = {"X-LingChat-Token": "secret"}

    with TestClient(app) as client:
        with client.websocket_connect("/ws?token=secret") as ws:
            session_id = ws.receive_json()["session"]
            ws.send_json(
                {"type": "user", "text": "store these", "attachments": attachments}
            )
            _drain_until(ws, "turn_end")

        transcript = client.get(f"/api/sessions/{session_id}", headers=headers).json()
        stored_message = transcript["messages"][0]
        stored = stored_message["attachments"]
        assert [attachment["kind"] for attachment in stored] == [
            "image",
            "file",
            "text",
            "binary",
        ]
        assert "data" in stored[0]
        assert all("data" not in attachment for attachment in stored[1:])
        assert all("fallback_text" not in attachment for attachment in stored)
        assert [attachment["size"] for attachment in stored] == [
            len(raw["image"]),
            len(raw["file"]),
            len(raw["text"]),
            len(raw["binary"]),
        ]
        assert all(attachment["download"] is True for attachment in stored)

        seq = stored_message["seq"]
        url = f"/api/sessions/{session_id}/messages/{seq}/attachments/2"
        assert client.get(url).status_code == 401
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert response.content == raw["text"]
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="notes.html";'
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"

        assert (
            client.get(
                f"/api/sessions/{'f' * 32}/messages/{seq}/attachments/0",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/sessions/{session_id}/messages/999/attachments/0",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/sessions/{session_id}/messages/{seq}/attachments/99",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/sessions/{session_id}/messages/{seq}/attachments/-1",
                headers=headers,
            ).status_code
            == 422
        )
