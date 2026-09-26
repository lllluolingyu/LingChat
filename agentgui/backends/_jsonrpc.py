"""Line-delimited JSON-RPC with an independent reader for server approvals."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ._procs import close_process

RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class JsonRpc:
    def __init__(self, handler: RequestHandler) -> None:
        self.handler = handler
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: asyncio.Queue[dict[str, Any] | Exception] = asyncio.Queue()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.counter = 0
        self.lock = asyncio.Lock()
        self.closed = False

    def _task(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self, command: list[str], cwd: str) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        self._task(self._read())
        self._task(self._stderr())

    async def send(self, msg: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin or self.closed:
            raise RuntimeError("app-server is not running")
        async with self.lock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> Any:
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)

    async def _answer(self, msg: dict[str, Any]) -> None:
        try:
            result = await self.handler(msg["method"], msg.get("params", {}))
            reply = {"id": msg["id"], "result": result}
        except asyncio.CancelledError:
            return
        except Exception as exc:
            reply = {"id": msg["id"], "error": {"code": -32601, "message": str(exc)}}
        try:
            await self.send(reply)
        except (OSError, RuntimeError):
            pass

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError("app-server sent a non-object frame")
                if "method" in msg:
                    if "id" in msg:
                        self._task(self._answer(msg))
                    else:
                        await self.notifications.put(msg)
                elif "id" in msg:
                    fut = self.pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result", {}))
            raise RuntimeError("codex app-server exited")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            await self.notifications.put(exc)

    async def _stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while line := await self.proc.stderr.readline():
                await self.notifications.put(
                    {
                        "method": "stderr",
                        "params": {"text": line.decode(errors="replace").strip()},
                    }
                )
        except (asyncio.CancelledError, ValueError):
            return

    async def close(self) -> None:
        self.closed = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("app-server connection closed"))
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.proc:
            await close_process(self.proc)
