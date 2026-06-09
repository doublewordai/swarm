from src import responses as R

MSG_RESP = {
    "id": "resp_1", "status": "completed", "model": "moonshotai/Kimi-K2.6",
    "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "hello world"}]},
    ],
    "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
}
TOOL_RESP = {
    "id": "resp_2", "status": "completed", "model": "m",
    "output": [{"type": "function_call", "call_id": "call_a", "name": "read_file",
                "arguments": "{\"path\": \"a.py\"}"}],
    "usage": {"input_tokens": 5, "output_tokens": 7},
}
LEN_RESP = {"id": "r", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [], "usage": {"input_tokens": 1, "output_tokens": 2}}


def test_text_of():
    assert R.text_of(MSG_RESP) == "hello world"


def test_function_calls_of():
    assert R.function_calls_of(TOOL_RESP) == [
        {"call_id": "call_a", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"}]


def test_usage_of():
    assert R.usage_of(MSG_RESP) == {"input_tokens": 11, "output_tokens": 3, "reasoning_tokens": 0}


def test_finish_of():
    assert R.finish_of(MSG_RESP) == "stop"
    assert R.finish_of(TOOL_RESP) == "tool_calls"
    assert R.finish_of(LEN_RESP) == "length"


def test_dispatch_blocking_uses_call(monkeypatch):
    seen = []

    def fake_call(client, **kw):
        seen.append(kw)
        return {"status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(R, "call", fake_call)
    out = R.dispatch(None, [{"model": "m", "input_items": []}],
                     service_tier="priority", background=False)
    assert len(out) == 1 and out[0]["status"] == "completed"
    assert seen[0]["service_tier"] == "priority" and seen[0]["background"] is False
