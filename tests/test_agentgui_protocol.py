from agentgui.protocol import ApprovalRequest, decision, frame, tool_kind


def test_frames_and_decisions_are_fail_closed():
    assert frame("text", delta="x").to_wire() == {"type": "text", "delta": "x"}
    approval = ApprovalRequest("shell", "Run", "echo hi")
    assert approval.to_wire()["options"] == ["once", "session", "deny"]
    assert decision("once") == "once"
    assert decision("yes") == "deny"
    assert tool_kind("mcp__server") == "mcp"
    assert tool_kind("Edit") == "edit"
