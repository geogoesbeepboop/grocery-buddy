"""Pure-logic tests for the rolling-window transcript truncation."""
from grocery_buddy.tools.conversation import _is_clean_user_start, truncate_messages


def _u(t):
    return {"role": "user", "content": t}


def _a(t):
    return {"role": "assistant", "content": [{"type": "text", "text": t}]}


def _a_tool():
    return {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "f", "input": {}}]}


def _tool_result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}


def test_no_truncation_under_cap():
    msgs = [_u("a"), _a("b")]
    assert truncate_messages(msgs, 40) == msgs


def test_cap_zero_or_negative_is_noop():
    msgs = [_u("a"), _a("b"), _u("c")]
    assert truncate_messages(msgs, 0) == msgs


def test_truncates_and_starts_clean():
    msgs = []
    for i in range(30):
        msgs += [_u(f"u{i}"), _a_tool(), _tool_result(), _a(f"a{i}")]
    out = truncate_messages(msgs, 10)
    assert len(out) <= 10
    # Never start a replay on an assistant message or an orphaned tool_result.
    assert _is_clean_user_start(out[0])
    assert out[0]["role"] == "user"
    # The most recent message is always preserved (tail intact).
    assert out[-1] == msgs[-1]


def test_clean_user_start_rejects_tool_result_and_assistant():
    assert _is_clean_user_start(_u("hi")) is True
    assert _is_clean_user_start(_a("hi")) is False
    assert _is_clean_user_start(_tool_result()) is False
