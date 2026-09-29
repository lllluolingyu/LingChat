"""LingCore features newer than the declared minimum (``lingcore>=0.3.0``).

``todo_write`` (``TodoUpdated`` + ``lingcore.todos``) arrives after 0.3.0. On
an older core the stand-ins below keep the AgentGUI backend importable: no
``TodoUpdated`` is ever emitted, so the placeholder class never matches, and
a ``todo_state`` row can only exist if a newer core wrote it, in which case it
is not rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingcore.events import TodoUpdated
    from lingcore.todos import todos_from_payload
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


__all__ = ["TodoUpdated", "todos_from_payload"]
