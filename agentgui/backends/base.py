"""The adapter contract every agent backend implements."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from agentgui.protocol import ApprovalRequest, Decision, Frame
from agentgui.store import SessionRecord, Store


@dataclass(frozen=True)
class Capabilities:
    edit_regenerate: bool = False
    fork: bool = False
    images: bool = False
    files: bool = False
    thinking: bool = False
    cost: bool = False
    fork_at_message: bool = False

    def to_wire(self) -> dict[str, bool]:
        return asdict(self)


@dataclass
class UserTurn:
    text: str
    attachments: list[Any] = field(default_factory=list)
    background: bool = False


ApproveFn = Callable[[ApprovalRequest], Awaitable[Decision]]


@runtime_checkable
class NativeEventBackend(Protocol):
    """A native agent owns its turns, including turns without a user prompt.

    Exactly one session-long consumer reads events. ``response_start`` and
    ``response_end`` delimit native responses; notices can arrive while idle.
    """

    @property
    def busy(self) -> bool: ...
    async def submit(self, inp: UserTurn) -> None: ...
    def events(self) -> AsyncIterator[Frame]: ...


@dataclass
class TurnContext:
    session: SessionRecord
    store: Store
    approve: ApproveFn


class AgentBackend(Protocol):
    capabilities: Capabilities

    async def start(self, session: SessionRecord, approve: ApproveFn) -> None: ...
    def run_turn(self, inp: UserTurn) -> AsyncIterator[Frame]: ...
    async def stop(self) -> list[Frame]: ...
    def edit(self, seq: int, text: str) -> AsyncIterator[Frame]: ...
    async def fork(self, through_seq: int | None) -> SessionRecord: ...
    def reconcile(self, status: list[dict[str, Any]]) -> None: ...
    async def close(self) -> None: ...


class BackendBase:
    capabilities = Capabilities()

    def __init__(self, store: Store) -> None:
        self.store = store
        self.session: SessionRecord
        self.approve: ApproveFn

    async def start(self, session: SessionRecord, approve: ApproveFn) -> None:
        self.session, self.approve = session, approve

    def save_native(self, native_id: str) -> None:
        self.session.native_id = native_id
        self.store.save(self.session)

    async def edit(self, seq: int, text: str) -> AsyncIterator[Frame]:
        raise ValueError("this backend does not support editing")
        yield  # pragma: no cover

    async def fork(self, through_seq: int | None) -> SessionRecord:
        raise ValueError("this backend does not support forking")

    def reconcile(self, status: list[dict[str, Any]]) -> None:
        """Rebuild GUI turns at turn end when native history is canonical."""

    def require_tip(self, through_seq: int | None) -> None:
        if not self.session.native_id:
            raise ValueError("send a message before forking")
        if (
            through_seq is not None
            and through_seq != self.store.next_seq(self.session.id) - 1
        ):
            raise ValueError(
                "this backend supports forking at the end of the conversation only"
            )
