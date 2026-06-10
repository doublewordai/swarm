"""Spec-clean Open Responses client + dispatch.

Codes against the Open Responses spec and trusts it: flat function tools,
caller-owned ``input`` item lists, ``background``+poll for async. No
provider-specific workarounds — if ``/v1/responses`` deviates from spec we
surface it rather than papering over it.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

TERMINAL = {"completed", "failed", "incomplete", "cancelled"}

PROVIDERS = {
    "doubleword": ("https://api.doubleword.ai/v1", "DOUBLEWORD_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}

_MAX_RETRIES = 3
_RETRY_DELAY = 1.0
# Seconds per request. The orchestrator's decomposition turn on a large repo is a big
# reasoning generation that legitimately runs into the minutes; too tight a timeout fails
# the whole run. Still bounded so a truly stalled call fails instead of hanging forever.
DEFAULT_TIMEOUT = 600.0


def make_client(provider: str = "doubleword", timeout: float = DEFAULT_TIMEOUT) -> OpenAI:
    """Build an OpenAI-compatible client for the given provider.

    A per-request ``timeout`` is set and the SDK's own retries are disabled
    (``_retry`` below owns retry policy), so one stalled generation fails fast
    and ``dispatch`` degrades gracefully instead of hanging the whole swarm.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    base_url, env = PROVIDERS[provider]
    key = os.environ.get(env)
    if not key:
        raise RuntimeError(
            f"{env} not set (run via `dw project run` or export a key from `dw keys create`)"
        )
    return OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)


def _retry(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        last = None
        for attempt in range(_MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except (
                ConnectionError,
                OSError,
                APIConnectionError,
                RateLimitError,
                APIStatusError,
            ) as exc:
                # Note: APITimeoutError is intentionally NOT retried — a timed-out
                # call is usually a stalled/oversized generation; retrying repeats
                # the stall. Let it propagate so dispatch records a failed resp.
                # Non-429 4xx (bad request, context overflow, auth) are deterministic:
                # retrying repeats the same failure, so give up immediately.
                status = getattr(exc, "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                last = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAY * (attempt + 1))
        raise last

    return wrapper


# --- pure parse helpers (operate on resp.model_dump() dicts) ---------------


def text_of(resp: dict) -> str:
    """Concatenate all assistant output_text from a Responses result."""
    parts = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for chunk in item.get("content", []):
                if isinstance(chunk, dict) and chunk.get("type") in ("output_text", "text"):
                    parts.append(chunk.get("text", ""))
    return "".join(parts)


def function_calls_of(resp: dict) -> list[dict]:
    """Extract function_call items as [{call_id, name, arguments}]."""
    out = []
    for item in resp.get("output", []):
        if item.get("type") == "function_call":
            out.append(
                {
                    "call_id": item.get("call_id") or item.get("id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                }
            )
    return out


def usage_of(resp: dict) -> dict:
    """Return measured token usage {input_tokens, output_tokens, reasoning_tokens}.

    Kimi K2.6 reports reasoning separately under
    ``output_tokens_details.reasoning_tokens`` (nonzero even at minimal effort), so
    we surface it — reasoning is billed as output, and ignoring it understates cost.
    """
    u = resp.get("usage") or {}
    details = u.get("output_tokens_details") or {}
    return {
        "input_tokens": u.get("input_tokens", 0) or 0,
        "output_tokens": u.get("output_tokens", 0) or 0,
        "reasoning_tokens": details.get("reasoning_tokens", 0) or 0,
    }


def finish_of(resp: dict) -> str:
    """Classify a response: tool_calls | stop | length | incomplete | error."""
    if function_calls_of(resp):
        return "tool_calls"
    status = resp.get("status", "completed")
    if status == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason")
        return "length" if reason == "max_output_tokens" else "incomplete"
    if status in ("failed", "cancelled"):
        return "error"
    return "stop"


# --- API calls -------------------------------------------------------------


@_retry
def call(
    client,
    *,
    model,
    input_items,
    tools=None,
    tool_choice=None,
    service_tier="priority",
    background=False,
    max_output_tokens=8192,
    temperature=0,
    reasoning_effort="minimal",
) -> dict:
    """Send one Open Responses turn (POST /v1/responses) and return its dict.

    ``reasoning_effort`` (minimal|low|medium|high) controls how much the model
    "thinks" before answering; minimal keeps reasoning models like Kimi K2.6 fast.
    Pass ``None``/empty to omit it (e.g. for non-reasoning models).
    """
    body = {
        "model": model,
        "input": input_items,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "service_tier": service_tier,
    }
    if tools:
        body["tools"] = tools  # flat function tools, per spec
    if tool_choice:
        body["tool_choice"] = tool_choice
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if background:
        body["background"] = True
    return client.responses.create(**body).model_dump()


def poll(client, response_id, interval: float = 3.0, timeout: float = 1800) -> dict:
    """Poll a background response until it reaches a terminal status.

    Individual ``retrieve`` calls are retried (transient errors); the *overall*
    timeout budget is never re-armed by a retry. Persistent retrieve failure
    degrades to a failed resp dict — poll never raises.
    """

    @_retry
    def _retrieve():
        return client.responses.retrieve(response_id).model_dump()

    waited = 0.0
    while True:
        try:
            resp = _retrieve()
        except Exception as exc:  # noqa: BLE001 - degrade, never hang the swarm
            return {"id": response_id, "status": "failed", "output": [], "usage": {},
                    "_error": f"poll retrieve failed: {exc}"}
        if resp.get("status") in TERMINAL:
            return resp
        time.sleep(interval)
        waited += interval
        if waited >= timeout:
            return {
                "id": response_id,
                "status": "failed",
                "output": [],
                "usage": {},
                "_error": "poll timeout",
            }


def dispatch(client, requests: list[dict], *, service_tier, background, max_concurrent=12) -> list[dict]:
    """Run a batch of turns, preserving input order.

    Blocking (realtime): fire all concurrently via a thread pool.
    Background (async): submit all, then poll each to completion.
    A failed request becomes a failed resp dict; this never raises. Each result
    carries ``_elapsed_s`` (wall time for that call) so callers can attribute
    latency to individual agents — the key signal when a swarm runs slow.
    """
    if not background:
        results: list = [None] * len(requests)

        def timed_call(req):
            t0 = time.monotonic()
            try:
                resp = call(client, service_tier=service_tier, background=False, **req)
            except Exception as exc:  # noqa: BLE001 - record, never block the wave
                resp = {"status": "failed", "output": [], "usage": {}, "_error": str(exc)}
            resp["_elapsed_s"] = round(time.monotonic() - t0, 2)
            return resp

        with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
            futs = {ex.submit(timed_call, req): i for i, req in enumerate(requests)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        return results

    t0 = time.monotonic()
    ids = []
    for req in requests:
        try:
            ids.append(call(client, service_tier=service_tier, background=True, **req).get("id"))
        except Exception:  # noqa: BLE001
            ids.append(None)
    out = []
    for rid in ids:
        if rid:
            resp = poll(client, rid)
        else:
            resp = {"status": "failed", "output": [], "usage": {}, "_error": "submit failed"}
        resp.setdefault("_elapsed_s", round(time.monotonic() - t0, 2))
        out.append(resp)
    return out
