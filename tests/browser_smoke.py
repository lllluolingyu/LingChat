"""Optional real-browser regression checks; see README for the Playwright command.

Serve the real ES modules/CSS with the production CSP through Playwright routes,
while replacing only HTTP transcript data and the WebSocket transport.
"""

import asyncio
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


def history(gui):
    messages = []
    turns = []
    for index in range(20):
        seq = index * 3
        text = f"Question {index}\n" + "line\n" * 4
        answer = f"Answer {index}\n" + "more detail\n" * 8
        messages.extend(
            [
                {"seq": seq, "role": "user", "text": text},
                {
                    "seq": seq + 1,
                    "role": "assistant",
                    "text": answer,
                    "tool_calls": [
                        {
                            "id": f"tool-{index}",
                            "name": "read_file",
                            "arguments": {"path": "test.txt"},
                        }
                    ],
                },
                {
                    "seq": seq + 2,
                    "role": "tool",
                    "id": f"tool-{index}",
                    "name": "read_file",
                    "ok": True,
                    "content": "tool output\n" * 10,
                },
            ]
        )
        turns.extend(
            [
                {"seq": seq, "frames": [{"type": "user", "text": text}]},
                {
                    "seq": seq + 1,
                    "frames": [
                        {"type": "thinking", "delta": "Thinking through this\n" * 5},
                        {"type": "text", "delta": answer},
                        {
                            "type": "tool_call",
                            "id": f"tool-{index}",
                            "name": "read_file",
                            "arguments": {"path": "test.txt"},
                        },
                        {
                            "type": "tool_result",
                            "id": f"tool-{index}",
                            "name": "read_file",
                            "ok": True,
                            "content": "tool output\n" * 10,
                            "diff": "--- a/test.txt\n+++ b/test.txt\n@@ -1 +1 @@\n-old\n+new",
                        },
                    ],
                },
            ]
        )
    return (
        {"title": "Browser regression", "turns": turns}
        if gui
        else {"title": "Browser regression", "messages": messages, "events": []}
    )


async def wait(page, expression):
    # CDP evaluate works under CSP; Playwright's wait_for_function uses eval.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if await page.evaluate(expression):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out: {expression}")


async def check(browser, gui):
    sys.path.insert(0, str(ROOT))
    from lingchat._web import CONTENT_SECURITY_POLICY

    web = ROOT / ("agentgui/web" if gui else "web")
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    payload = history(gui)
    hello = {
        "type": "hello",
        "history": payload,
        "running": False,
        "replay": [],
        "approvals": [],
        "session": "session-a",
        "title": "Browser regression",
        "agent": "Test",
        "model": "fake",
        "backend": "claude",
        "autonomy": "ask",
        "workspace": "/tmp",
        "commands": [{"name": "/plan", "description": "Plan"}, {"name": "/plugin"}],
        "limits": {"max_attachments": 1},
        "capabilities": {
            "images": True,
            "files": False,
            "edit_regenerate": True,
            "fork": True,
            "fork_at_message": True,
        },
    }
    await page.add_init_script(
        """(hello => {
      window.__sent = []; window.__sockets = []; window.__csp = []; window.__reads = [];
      addEventListener('securitypolicyviolation', event => __csp.push(event.violatedDirective));
      window.WebSocket = class extends EventTarget {
        static CONNECTING = 0; static OPEN = 1; static CLOSED = 3;
        constructor(url) {
          super(); this.readyState = 0; __sockets.push(this);
          setTimeout(() => {
            this.readyState = 1; this.dispatchEvent(new Event('open'));
            this.emit({...hello, session: new URL(url).searchParams.get('session') || 'session-a'});
          }, 0);
        }
        emit(data) { this.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(data)})); }
        send(data) { __sent.push(JSON.parse(data)); }
        close() { this.readyState = 3; setTimeout(() => this.dispatchEvent(new CloseEvent('close')), 0); }
      };
      const read = FileReader.prototype.readAsDataURL;
      FileReader.prototype.readAsDataURL = function(file) { __reads.push(() => read.call(this, file)); };
      window.__emit = data => __sockets.at(-1).emit(data);
    })("""
        + json.dumps(hello)
        + ");"
    )
    history_gate = None
    history_requested = asyncio.Event()

    async def serve(route):
        from urllib.parse import urlsplit

        path = urlsplit(route.request.url).path
        if path == "/api/sessions":
            await route.fulfill(
                json={
                    "enabled": True,
                    "sessions": [
                        {
                            "id": value,
                            "title": value,
                            "updated_at": "2026-10-18T10:00:00Z",
                            "message_count": 60,
                            "backend": "claude",
                        }
                        for value in ("session-a", "session-b")
                    ],
                }
            )
        elif path.startswith("/api/sessions/"):
            if history_gate:
                history_requested.set()
                await history_gate.wait()
            await route.fulfill(json=payload)
        elif path == "/api/models":
            await route.fulfill(
                json={
                    "models": [{"id": "test", "label": "Test", "backend": "claude"}],
                    "workspace": "/tmp",
                }
            )
        else:
            target = (
                ROOT / "lingchat" / path.lstrip("/")
                if path.startswith("/shared/")
                else web / (path.lstrip("/") or "index.html")
            )
            assert target.is_file(), target
            policy = CONTENT_SECURITY_POLICY
            await route.fulfill(
                body=target.read_bytes(),
                content_type=mimetypes.guess_type(target)[0]
                or "application/octet-stream",
                headers={"Content-Security-Policy": policy},
            )

    await page.route("http://lingchat.test/**", serve)
    await page.goto("http://lingchat.test/#session-a")
    await wait(page, "document.querySelectorAll('.tool-card').length === 20")
    await page.locator("#input").fill("/plan")
    await page.locator("#input").press("Enter")
    assert await page.evaluate("__sent.at(-1).text") == "/plan"
    await page.evaluate("__emit({type:'text',text:'First settled para\\n\\nSecond '})")
    await wait(
        page,
        "document.querySelectorAll('.row.agent:last-child .agent-body p').length === 2",
    )
    await page.evaluate(
        "document.querySelector('.row.agent:last-child .agent-body p').dataset.kept = '1'"
    )
    await page.evaluate("__emit({type:'text',text:'growing **tail**'})")
    await wait(
        page,
        "document.querySelector('.row.agent:last-child .agent-body strong')?.textContent === 'tail'",
    )
    assert await page.evaluate(
        "document.querySelector('.row.agent:last-child .agent-body p').dataset.kept === '1'"
    )
    await page.evaluate("__emit({type:'turn_end'})")
    await wait(page, "!document.querySelector('#attach').disabled")

    # Real textarea edit keeps whitespace and ignores composing Ctrl+Enter.
    await page.locator(".user-actions .edit-btn:not(.fork-btn)").first.click()
    edit = page.locator(".user-edit-input")
    text = "  /plan  indented\n\n"
    await edit.fill(text)
    before = await page.evaluate("__sent.length")
    await edit.dispatch_event(
        "keydown", {"key": "Enter", "ctrlKey": True, "isComposing": True}
    )
    assert await page.evaluate("__sent.length") == before
    await edit.press("Control+Enter")
    assert await page.evaluate("__sent.at(-1).text") == text
    await page.evaluate("__emit({type:'turn_end'})")
    await wait(
        page,
        "document.querySelectorAll('.tool-card').length === 20 && !document.querySelector('#attach').disabled",
    )

    # Interact while the history fetch is pending: preserve the latest view,
    # rather than the view at the moment turn_end arrived.
    history_gate = asyncio.Event()
    await page.evaluate("__emit({type:'turn_end'})")
    await asyncio.wait_for(history_requested.wait(), 5)
    await page.evaluate("""() => {
      document.querySelector('[data-view-key="tool:tool-5"].tool-card').open = true;
      const thinking = document.querySelector('.thinking-block'); if (thinking) thinking.open = true;
      const m = document.querySelector('#messages'); m.scrollTop = 900;
      m.dispatchEvent(new Event('scroll'));
    }""")
    await page.wait_for_timeout(50)
    top = await page.locator("#messages").evaluate("e => e.scrollTop")
    history_gate.set()
    await wait(page, "!document.querySelector('#attach').disabled")
    history_gate = None
    assert await page.locator('[data-view-key="tool:tool-5"].tool-card').evaluate(
        "e => e.open"
    )
    assert abs(await page.locator("#messages").evaluate("e => e.scrollTop") - top) < 3
    if gui:
        await page.locator("#lang-toggle").click()
        await wait(page, "!document.querySelector('#attach').disabled")
        assert await page.locator(".thinking-block").first.evaluate("e => e.open")
        assert await page.locator('[data-view-key="tool:tool-5"].tool-card').evaluate(
            "e => e.open"
        )
        assert await page.locator("#messages").evaluate(
            "e => e.scrollTop < e.scrollHeight - e.clientHeight - 90"
        )
        assert await page.locator(".diff-view").first.locator(".diff-add").count() == 1
        assert (
            await page.locator(".diff-view").first.locator(".diff-remove").count() == 1
        )

    # Reads block both Send and keyboard submit; second paste/file choice queues.
    await page.locator("#input").fill("with attachment")
    upload = {"name": "picture.png", "mimeType": "", "buffer": b"png-data"}
    await page.locator("#file-input").set_input_files(upload)
    await wait(page, "__reads.length === 1")
    assert await page.locator("#send").is_disabled()
    before = await page.evaluate("__sent.length")
    await page.locator("#input").press("Enter")
    assert await page.evaluate("__sent.length") == before
    await page.locator("#file-input").set_input_files(upload)
    await page.evaluate("__reads.shift()()")
    await wait(page, "!document.querySelector('#send').disabled")
    assert await page.locator(".attachment-chip").count() == 1
    assert await page.evaluate("__reads.length") == 0
    await page.locator("#input").press("Enter")
    assert await page.evaluate("__sent.at(-1).attachments[0].media_type") == "image/png"
    await page.evaluate("__emit({type:'turn_end'})")
    await wait(page, "!document.querySelector('#attach').disabled")

    # Native modal traps focus, Escape denies just one request, reset closes it.
    request = (
        {
            "type": "approval",
            "title": "Approve?",
            "detail": "Tool",
            "options": ["once", "session", "deny"],
        }
        if gui
        else {"type": "confirm", "command": "ls", "allowlist_pattern": "ls"}
    )
    await page.evaluate(
        "r => { __emit({...r,id:'one'}); __emit({...r,id:'two'}); }", request
    )
    assert await page.locator("#confirm-deny").evaluate(
        "e => e === document.activeElement"
    )
    await page.locator("#input").evaluate("e => e.focus()")
    assert await page.locator("#confirm-deny").evaluate(
        "e => e === document.activeElement"
    )
    for _ in range(6):
        await page.keyboard.press("Tab")
        # Chromium includes browser chrome at the end of its native modal tab
        # cycle (activeElement becomes body), but background controls stay inert.
        assert await page.evaluate(
            "document.activeElement === document.body || !!document.activeElement.closest('#confirm')"
        )
    await page.keyboard.press("Escape")
    assert await page.evaluate("__sent.at(-1).id") == "one"
    assert await page.locator("#confirm").evaluate("e => e.open")
    await page.locator("#confirm-allow").click()
    assert await page.evaluate("__sent.at(-1).id") == "two"
    assert not await page.locator("#confirm").evaluate("e => e.open")
    await page.evaluate("r => __emit({...r,id:'three'})", request)
    await page.evaluate("__emit({type:'cancelled'})")
    assert not await page.locator("#confirm").evaluate("e => e.open")

    # Native session button switches via keyboard; rename/delete remain siblings.
    # A pending read from this session must also be discarded on the switch.
    await page.locator("#file-input").set_input_files(upload)
    await wait(page, "__reads.length === 1")
    select = page.locator(".session-select").nth(1)
    await select.focus()
    assert await page.locator(".session-actions button").nth(2).is_visible()
    await select.press("Space")
    await wait(
        page,
        "location.hash === '#session-b' && __sockets.length === 2 && !document.querySelector('#attach').disabled",
    )
    await page.evaluate("__reads.shift()()")
    await page.wait_for_timeout(50)
    assert await page.locator(".attachment-chip").count() == 0
    assert await page.locator(".session-select button").count() == 0

    # Reattach synchronously to a running turn: replay the partial answer and
    # pending approval before new deltas arrive, without an HTTP replay race.
    resumed = {
        **hello,
        "session": "session-b",
        "history": payload,
        "running": True,
        "replay": [{"type": "text", "delta" if gui else "text": "Saved progress"}],
        "approvals": [{**request, "id": "resume-approval"}],
    }
    await page.evaluate("hello => __emit(hello)", resumed)
    assert await page.locator("#stop").is_visible()
    assert await page.locator("#send").is_hidden()
    assert await page.locator("#confirm").evaluate("e => e.open")
    await page.locator("#confirm-allow").click()
    assert await page.evaluate("__sent.at(-1).id") == "resume-approval"
    await page.evaluate("__emit({type:'text',text:' continues',delta:' continues'})")
    await page.wait_for_timeout(100)
    assert await page.get_by_text("Saved progress continues", exact=True).count() == 1
    await page.evaluate("__emit({type:'cancelled'}); __emit({type:'turn_end'})")
    await wait(page, "!document.querySelector('#attach').disabled")

    # An automatic monitor turn can arrive while the prior transcript reload
    # is still in flight. Its snapshot must win, and the user's draft survives.
    await page.locator("#input").fill("draft kept while watching")
    history_gate = asyncio.Event()
    history_requested.clear()
    await page.evaluate("__emit({type:'turn_end'})")
    await asyncio.wait_for(history_requested.wait(), 3)
    monitor_text = "Monitor event: build passed"
    monitor_history = (
        {
            "turns": [
                {
                    "seq": 900,
                    "role": "user",
                    "frames": [
                        {"type": "user", "name": "monitor", "text": monitor_text}
                    ],
                }
            ]
        }
        if gui
        else {
            "messages": [
                {"seq": 900, "role": "user", "name": "monitor", "text": monitor_text}
            ],
            "events": [],
        }
    )
    await page.evaluate("history => __emit({type:'monitor', history})", monitor_history)
    await page.evaluate(
        "__emit({type:'text',text:'Automatic response',delta:'Automatic response'})"
    )
    history_gate.set()
    history_gate = None
    await page.wait_for_timeout(100)
    assert await page.locator("#input").input_value() == "draft kept while watching"
    assert await page.get_by_text(monitor_text, exact=True).count() == 1
    assert await page.get_by_text("Automatic response", exact=True).count() == 1
    assert await page.locator("#stop").is_visible()
    await page.evaluate("__emit({type:'turn_end'})")
    await wait(page, "!document.querySelector('#attach').disabled")
    await page.locator("#input").fill("")

    if gui:
        # Native task lifecycle notices arrive even when Claude is idle. They
        # update history without enabling Stop or destroying the user's draft.
        await page.locator("#input").fill("draft during native notification")
        notice_history = {
            "turns": [
                {
                    "seq": 901,
                    "role": "assistant",
                    "frames": [
                        {
                            "type": "notice",
                            "source": "claude_task",
                            "level": "info",
                            "text": "Task watch-1 completed",
                        },
                        {
                            "type": "notice",
                            "source": "claude_event",
                            "level": "info",
                            "text": "Native monitor event",
                        },
                    ],
                }
            ]
        }
        await page.evaluate(
            "history => __emit({type:'monitor', history, running:false})",
            notice_history,
        )
        assert await page.get_by_text("Task watch-1 completed", exact=True).count() == 1
        assert (
            await page.locator(".note.monitor")
            .filter(has_text="Native monitor event")
            .count()
            == 1
        )
        assert await page.locator("#stop").is_hidden()
        assert (
            await page.locator("#input").input_value()
            == "draft during native notification"
        )
        await page.locator("#input").fill("")

    # A provider limit preserves partial text and exactly one warning, both
    # live and after history reload (including another reload later).
    for index, partial in enumerate(["Partial answer survives reload.", ""]):
        error = {
            "type": "error",
            "message": f"Model response incomplete: output limit (attempt {index}).",
            "finish_reason": "length",
            "partial_content": partial,
        }
        await page.locator("#input").fill("Show a partial response")
        await page.locator("#input").press("Enter")
        await page.evaluate("__emit({type:'text',text:'SECRET\\n\\nMore SECRET'})")
        await page.evaluate("error => __emit(error)", error)
        assert "SECRET" not in await page.locator("#thread").inner_text()
        assert not await page.locator('.copy-btn[data-raw*="SECRET"]').count()
        if partial:
            assert await page.get_by_text(partial, exact=True).count() == 1
        warning = page.locator(".note.error").filter(has_text=error["message"])
        assert await warning.count() == 1
        if gui:
            payload["turns"].append(
                {
                    "seq": 61 + index,
                    "frames": [{"type": "text", "delta": partial}, error],
                }
            )
        else:
            payload["messages"].append(
                {
                    "seq": 61 + index,
                    "role": "assistant",
                    "text": partial,
                    "error": error["message"],
                    "finish_reason": "length",
                    "tool_calls": [],
                }
            )
        for _ in range(2):
            await page.evaluate("__emit({type:'turn_end'})")
            await wait(page, "!document.querySelector('#attach').disabled")
            if partial:
                assert await page.get_by_text(partial, exact=True).count() == 1
            assert await warning.count() == 1
            assert "SECRET" not in await page.locator("#thread").inner_text()

    if gui:
        await page.locator("#new-chat").click()
        await wait(page, "document.querySelector('#new-model').options.length === 1")
        assert await page.locator("#new-model optgroup").count() == 1
        await page.locator("#new-chat-cancel").click()
    assert errors == [], errors
    assert await page.evaluate("__csp") == []
    print(
        f"{'AgentGUI' if gui else 'LingChat'}: browser regressions passed", flush=True
    )
    await context.close()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.environ.get("CHROMIUM_EXECUTABLE")
        )
        try:
            await check(browser, False)
            await check(browser, True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
