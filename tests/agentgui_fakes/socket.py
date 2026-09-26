"""Drive the actual ASGI WebSocket endpoint without network ports or threads."""

import asyncio
import json


class Socket:
    def __init__(self, app, sid, token="secret", origin="http://test"):
        self.app, self.sid, self.token, self.origin = app, sid, token, origin
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()

    async def __aenter__(self):
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "path": "/ws",
            "raw_path": b"/ws",
            "root_path": "",
            "scheme": "ws",
            "query_string": f"session={self.sid}&token={self.token}".encode(),
            "headers": [(b"host", b"test"), (b"origin", self.origin.encode())],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "subprotocols": [],
        }
        self.task = asyncio.create_task(
            self.app(scope, self.incoming.get, self.outgoing.put)
        )
        await self.incoming.put({"type": "websocket.connect"})
        self.handshake = await asyncio.wait_for(self.outgoing.get(), 4)
        return self

    async def send(self, msg):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(msg)})

    async def recv(self):
        item = await asyncio.wait_for(self.outgoing.get(), 5)
        if item["type"] != "websocket.send":
            return item
        return json.loads(item["text"])

    async def until(self, kind):
        messages = []
        while True:
            msg = await self.recv()
            messages.append(msg)
            if msg["type"] == kind:
                return messages

    async def __aexit__(self, *exc):
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 15)
