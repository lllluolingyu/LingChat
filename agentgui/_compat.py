"""LingCore features newer than the declared minimum (``lingcore>=0.3.0``).

``todo_write`` (``TodoUpdated`` + ``lingcore.todos``) arrives after 0.3.0, and
per-turn stats (``TurnStatsReported`` + ``turn_stats`` session events) after
0.4.1. On an older core the stand-ins below keep the AgentGUI backend importable: the
placeholder events are never emitted, so they never match, and a stored row of
either kind can only exist if a newer core wrote it, in which case it is not
rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingcore.events import TodoUpdated, TurnStatsReported
    from lingcore.todos import todos_from_payload
    from lingcore.usage import turn_stats_from_payload
else:
    try:
        from lingcore.events import TodoUpdated
        from lingcore.todos import todos_from_payload
    except ImportError:  # LingCore 0.3.0

        class TodoUpdated:
            __match_args__ = ("todos",)
            todos: tuple = ()

        def todos_from_payload(payload):
            return None

    try:
        from lingcore.events import TurnStatsReported
        from lingcore.usage import turn_stats_from_payload
    except ImportError:  # LingCore 0.4.1

        class TurnStatsReported:
            __match_args__ = ("stats",)
            stats = None

        def turn_stats_from_payload(payload):
            return None


__all__ = [
    "TodoUpdated",
    "TurnStatsReported",
    "todos_from_payload",
    "turn_stats_from_payload",
]
