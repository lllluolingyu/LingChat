from __future__ import annotations

from types import SimpleNamespace

import pytest
from lingcore.events import PluginNotice
from lingcore.message import UserInput
from lingcore.plugins.commands import Command, CommandCatalog

from agentgui.backends.lingcore_backend import event_frame
from lingchat.server import WebSession, _event_to_msg


def test_plugin_notice_wire_mapping():
    notice = PluginNotice("policy", "before_tool", "blocked", "Approval required")
    payload = _event_to_msg(notice)
    assert payload == {
        "type": "plugin_notice",
        "plugin": "policy",
        "hook": "before_tool",
        "action": "blocked",
        "message": "Approval required",
    }
    assert event_frame(notice).to_wire() == payload


def test_lingchat_resolves_command_and_keeps_attachments():
    bridge = object.__new__(WebSession)
    bridge.agent = SimpleNamespace(
        commands=CommandCatalog([Command("plan", "Plan $ARGUMENTS")])
    )
    incoming = UserInput(text="/plan  work")
    resolved = bridge._resolve_input(incoming)
    assert resolved.text == "Plan work"
    assert resolved.display_text == incoming.text
    assert resolved.attachments == incoming.attachments


def test_lingcore_backend_resolves_commands_and_closes(tmp_path):
    import asyncio

    from lingcore.llm import LLMChunk

    from agentgui.backends.base import UserTurn
    from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache
    from agentgui.catalog import ModelEntry
    from agentgui.store import Store

    captured = []

    class FakeLLM:
        async def stream(self, messages, tools=None):
            captured.append(messages[-1].content)
            yield LLMChunk(text_delta="done")
            yield LLMChunk(finish_reason="stop")

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        profile = tmp_path / "config.yaml"
        profile.write_text(
            f"name: fake\nworkspace: {workspace}\nllm:\n  model: fake\ntools: []\n"
        )
        (tmp_path / "commands").mkdir()
        (tmp_path / "commands" / "review.md").write_text("Review $ARGUMENTS")
        store = Store(tmp_path / "gui.db")
        cache = ProfileCache()
        session = store.create(
            ModelEntry("fake", "Fake", "lingcore", options={"profile": str(profile)}),
            str(workspace),
        )
        backend = LingCoreBackend(store, cache, FakeLLM)
        await backend.start(session, lambda request: asyncio.sleep(0, result="deny"))
        assert backend.commands_metadata()[0]["name"] == "/review"
        notices = []
        agent = backend.agent
        original_close = agent.aclose

        async def close():
            notices.append(agent)
            await original_close()

        agent.aclose = close
        frames = [f async for f in backend.run_turn(UserTurn("/review  src"))]
        assert frames[-1].type == "final"
        user = store.turns(session.id)[0]["frames"][0]
        assert user["text"] == "/review  src"
        await backend.close()
        assert notices == [agent]
        cache.close()
        store.close()

    asyncio.run(run())
    assert captured == ["Review src"]


def test_lingchat_edit_reexpands_and_closes_replaced_and_current_agents(tmp_path):
    import asyncio

    from lingcore.config import AgentProfile
    from lingcore.llm import LLMChunk
    from lingcore.sessions import SessionStore, new_session_id

    seen = []

    class FakeLLM:
        async def stream(self, messages, tools=None):
            seen.append(messages[-1].content)
            yield LLMChunk(text_delta="done")
            yield LLMChunk(finish_reason="stop")

    class Socket:
        async def send_json(self, message):
            pass

    async def run():
        path = tmp_path / "config.yaml"
        path.write_text(
            f"name: test\nworkspace: {tmp_path}\nllm:\n  model: fake\ntools: []\n"
        )
        (tmp_path / "commands").mkdir()
        (tmp_path / "commands" / "plan.md").write_text("Plan $ARGUMENTS")
        profile = AgentProfile.load(path)
        store = SessionStore(tmp_path / "sessions.db", profile_name="test")
        session = WebSession(
            Socket(),
            profile,
            tmp_path,
            llm_factory=FakeLLM,
            store=store,
            session_id=new_session_id(),
        )
        await session._run_turn(session._resolve_input(UserInput(text="/plan initial")))
        previous = session.agent
        closed = []
        original_close = previous.aclose

        async def close_previous():
            closed.append(previous)
            await original_close()

        previous.aclose = close_previous
        await session._edit_turn(0, "/plan changed")
        await asyncio.gather(*session._tasks)
        assert closed == [previous]
        current = session.agent
        current_close = current.aclose

        async def close_current():
            closed.append(current)
            await current_close()

        current.aclose = close_current
        await session.aclose()
        assert closed == [previous, current]
        assert (
            store.message_records(session._session_id)[0].message.input_text
            == "/plan changed"
        )
        store.close()

    asyncio.run(run())
    assert seen == ["Plan initial", "Plan changed"]


def test_backend_expansion_keeps_attachment_batch_validation():
    import asyncio

    import pytest
    from lingcore.events import Final
    from lingcore.media import attachment_from_bytes

    from agentgui.backends.base import UserTurn
    from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache

    class Agent:
        commands = CommandCatalog([Command("plan", "Plan $ARGUMENTS")])

        async def run(self, incoming):
            yield Final("done")

    async def run():
        backend = LingCoreBackend(SimpleNamespace(), ProfileCache())
        backend.agent = Agent()
        attachment = attachment_from_bytes(b"notes", name="notes.txt")
        with pytest.raises(ValueError, match="attachments"):
            _ = [
                frame
                async for frame in backend.run_turn(
                    UserTurn("/plan work", [attachment] * 17)
                )
            ]
        assert backend.task is None

    asyncio.run(run())


@pytest.mark.parametrize("disconnect", [False, True])
def test_websession_stop_drains_notices_before_finalize(disconnect):
    import asyncio

    from lingcore.events import TurnCancelled

    async def run():
        output = []
        notices = [PluginNotice("policy", "before_tool", "asked", "Approve?")]

        async def blocked():
            await asyncio.Event().wait()

        task = asyncio.create_task(blocked())
        await asyncio.sleep(0)

        def cancel():
            task.cancel()
            return True

        def drain():
            result = notices.copy()
            notices.clear()
            return result

        def finalize():
            notices.clear()
            return TurnCancelled()

        async def send(value):
            output.append(value)

        session = object.__new__(WebSession)
        session.agent = SimpleNamespace(
            cancel_turn=cancel,
            finalize_cancelled_turn=finalize,
            drain_plugin_notices=drain,
            drain_usage=lambda: [],
        )
        session._turn_task = task
        session._tasks = {task}
        session._turn_terminal = False
        session.ws = SimpleNamespace(send_json=send)
        if disconnect:
            await session.aclose()
        else:
            assert await session.stop_turn()
        expected = ["plugin_notice", "cancelled"] + ([] if disconnect else ["turn_end"])
        assert [message["type"] for message in output] == expected

    asyncio.run(run())


def test_agentgui_stop_drains_notices_before_finalize():
    import asyncio

    from lingcore.events import TurnCancelled

    from agentgui.backends.lingcore_backend import LingCoreBackend, ProfileCache

    async def run():
        notices = [PluginNotice("policy", "before_tool", "asked", "Approve?")]

        def drain():
            result = notices.copy()
            notices.clear()
            return result

        def finalize():
            notices.clear()
            return TurnCancelled()

        backend = LingCoreBackend(SimpleNamespace(), ProfileCache())
        backend.agent = SimpleNamespace(
            cancel_turn=lambda: True,
            finalize_cancelled_turn=finalize,
            drain_plugin_notices=drain,
            drain_usage=lambda: [],
        )
        frames = await backend.stop()
        assert [value.type for value in frames] == ["plugin_notice", "cancelled"]

    asyncio.run(run())
