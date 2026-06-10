"""Briefs: what makes the generic swarm do a specific job.

A Brief bundles the prompts, the result schema, the per-role tool selection, and the
dedupe/verify hooks for one use case. The engine (`src/engine.py`) is brief-agnostic;
``audit`` is just the first brief. Add a module here, build a `Brief`, `register(...)`
it, and `swarm run <name>` works — no engine changes. See README "Write your own brief".
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Brief:
    name: str
    description: str
    orchestrator_prompt: str
    worker_prompt: str
    synthesis_prompt: str
    result_schema: dict                      # JSON schema for ONE result item
    result_key: str = "results"              # array key for submit_results + the results file
    verifier_prompt: str | None = None       # None ⇒ skip the verify stage
    solo_prompt: str | None = None            # whole-repo system prompt for --solo (else worker_prompt)
    worker_tools: tuple = ("read_file", "grep")
    verifier_tools: tuple = ()
    dedupe_key: Callable | None = None        # item -> hashable key (None ⇒ no dedupe)
    rank: Callable | None = None              # item -> comparable (keep the max per dedupe key)
    submit_description: str = "Submit your results and finish. Return results only — do not narrate."


_REGISTRY: dict[str, Brief] = {}


def register(brief: Brief) -> Brief:
    _REGISTRY[brief.name] = brief
    return brief


def get_brief(name: str) -> Brief:
    if name not in _REGISTRY:
        raise KeyError(f"unknown brief: {name!r} (available: {', '.join(list_briefs())})")
    return _REGISTRY[name]


def list_briefs() -> list[str]:
    return sorted(_REGISTRY)


# Register the built-in briefs (import for side effects).
from . import audit, onboarding  # noqa: E402,F401
