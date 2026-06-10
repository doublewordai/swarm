import threading
import time

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from src import responses as R


def _status_error(code: int) -> APIStatusError:
    resp = httpx.Response(code, request=httpx.Request("POST", "http://test"))
    cls = RateLimitError if code == 429 else APIStatusError
    return cls("boom", response=resp, body=None)

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


def test_default_timeout_is_generous_for_reasoning_calls():
    # A big orchestrator decomposition on a reasoning model legitimately takes
    # minutes; 180s was too tight and timed out large-repo runs.
    assert R.DEFAULT_TIMEOUT >= 300


def test_make_client_honours_timeout(monkeypatch):
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")
    client = R.make_client("doubleword", timeout=42.0)
    assert client.timeout == 42.0


def test_retry_gives_up_immediately_on_client_error():
    """A 4xx (e.g. context overflow) is deterministic — retrying just wastes time."""
    calls = {"n": 0}

    @R._retry
    def always_400():
        calls["n"] += 1
        raise _status_error(400)

    with pytest.raises(APIStatusError):
        always_400()
    assert calls["n"] == 1


def test_retry_retries_rate_limit(monkeypatch):
    monkeypatch.setattr(R, "_RETRY_DELAY", 0.0)
    calls = {"n": 0}

    @R._retry
    def flaky_429():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _status_error(429)
        return "ok"

    assert flaky_429() == "ok"
    assert calls["n"] == 2


def test_retry_retries_server_error(monkeypatch):
    monkeypatch.setattr(R, "_RETRY_DELAY", 0.0)
    calls = {"n": 0}

    @R._retry
    def flaky_500():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _status_error(500)
        return "ok"

    assert flaky_500() == "ok"
    assert calls["n"] == 2


def test_dispatch_bounds_concurrency(monkeypatch):
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fake_call(client, **kw):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return {"status": "completed", "output": [], "usage": {}}

    monkeypatch.setattr(R, "call", fake_call)
    out = R.dispatch(None, [{"model": "m", "input_items": []} for _ in range(6)],
                     service_tier="priority", background=False, max_concurrent=2)
    assert len(out) == 6
    assert state["peak"] <= 2


class _FlakyRetrieveClient:
    """retrieve() raises a transient 500 once, then completes."""

    def __init__(self, fail_times=1):
        self.fails_left = fail_times
        self.calls = 0

        outer = self

        class _Responses:
            def retrieve(self, rid):
                outer.calls += 1
                if outer.fails_left > 0:
                    outer.fails_left -= 1
                    raise _status_error(500)

                class _R:
                    @staticmethod
                    def model_dump():
                        return {"id": rid, "status": "completed", "output": [], "usage": {}}

                return _R()

        self.responses = _Responses()


def test_poll_survives_transient_retrieve_error(monkeypatch):
    monkeypatch.setattr(R, "_RETRY_DELAY", 0.0)
    out = R.poll(_FlakyRetrieveClient(fail_times=1), "resp_x", interval=0.0)
    assert out["status"] == "completed"


def test_poll_returns_failed_on_persistent_retrieve_error(monkeypatch):
    """poll must degrade to a failed resp dict, not raise and not loop forever."""
    monkeypatch.setattr(R, "_RETRY_DELAY", 0.0)
    out = R.poll(_FlakyRetrieveClient(fail_times=99), "resp_x", interval=0.0)
    assert out["status"] == "failed"
    assert "resp_x" == out["id"]


def test_dispatch_attaches_elapsed_realtime(monkeypatch):
    monkeypatch.setattr(R, "call", lambda client, **kw: {
        "status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 1}})
    out = R.dispatch(None, [{"model": "m", "input_items": []}],
                     service_tier="priority", background=False)
    assert isinstance(out[0]["_elapsed_s"], float)


def test_dispatch_attaches_elapsed_on_failure(monkeypatch):
    def boom(client, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(R, "call", boom)
    out = R.dispatch(None, [{"model": "m", "input_items": []}],
                     service_tier="priority", background=False)
    assert out[0]["status"] == "failed"
    assert "_elapsed_s" in out[0]


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
