"""Agent runtimes — the swappable backend the engine drives.

The engine is deliberately decoupled from *how* a turn is executed. It speaks one
small contract, the ``Runtime`` protocol below, and everything else in the engine
operates on the **Open Responses shape** (``output`` items, ``usage``,
``status``) via the pure parse helpers in ``responses`` — so a runtime's only job
is to turn a batch of requests into Responses-shaped result dicts.

Two runtimes:

- ``responses`` (default, native) — talks the Open Responses API directly
  (``/v1/responses``, background+poll). It already satisfies this protocol; the
  engine uses it unless ``SwarmConfig.runtime`` is set.
- ``opencode`` (v2, planned) — drives an ``opencode serve`` HTTP session and maps
  its message parts back to the Responses shape. See
  ``docs/adr-001-opencode-runtime.md``.

Why this is the whole seam: the engine funnels every model call through a single
``_dispatch`` indirection, and its parsers (``text_of`` / ``function_calls_of`` /
``usage_of`` / ``finish_of``) are shape-based, so a new backend that returns the
same dict shape plugs in without touching the orchestration logic.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Runtime(Protocol):
    """What the engine needs from a backend. ``responses`` is the reference impl."""

    def make_client(self, provider: str = ..., timeout: float = ...):
        """Return an opaque client handle the runtime's own ``dispatch`` understands."""
        ...

    def dispatch(self, client, requests: list[dict], *, service_tier: str,
                 background: bool, max_concurrent: int) -> list[dict]:
        """Execute a batch of turns, in input order, and return Responses-shaped
        result dicts (``{status, output: [...items], usage, _elapsed_s}``). Must not
        raise — a failed turn becomes a ``status: "failed"`` dict. ``requests`` are
        the dicts built by ``engine._req`` (``model``, ``input_items``, ``tools``,
        ``temperature``, ``max_output_tokens``, ``reasoning_effort``, ``tool_choice``).
        """
        ...
