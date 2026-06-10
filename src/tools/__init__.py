"""Flat Open Responses tool schemas + execution dispatch.

Engine-handled (deferred) tools: ``dispatch_workers`` (orchestrator), ``submit_results``
(worker terminal — its schema is per-brief, built by ``submit_results_tool``), and
``submit_verdict`` (verifier terminal). The capability tools
(``read_file``/``grep``/``run_sast``/``check_advisory``/``web_search``/``read_page``)
run immediately. A brief selects which capability tools its workers/verifiers get.
"""

import json

from . import advisory, repo, sast, search

DEFERRED = "__DEFERRED__"
_DEFERRED_NAMES = {"dispatch_workers", "submit_results", "submit_verdict",
                   "create_subagent", "assign_task"}

# Kimi's trained swarm interface (K2.5 tech report, Appendix E.8). Schemas kept as
# close to the published ones as possible so K2.5/K2.6 runs ride the PARL-trained
# prior; `files` on assign_task is our one extension (preloads bounded context).
CREATE_SUBAGENT = {
    "type": "function",
    "name": "create_subagent",
    "description": "Create a custom subagent with specific system prompt and name for reuse.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique name for this agent configuration"},
            "system_prompt": {"type": "string",
                              "description": "System prompt defining the agent's role, capabilities, and boundaries"},
        },
        "required": ["name", "system_prompt"],
    },
}
ASSIGN_TASK = {
    "type": "function",
    "name": "assign_task",
    "description": ("Launch a new agent.\nUsage notes:\n"
                    "1. You can launch multiple agents concurrently whenever possible, "
                    "to maximize performance;\n"
                    "2. When the agent is done, it will return a single message back to you."),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "Specify which created agent to use."},
            "prompt": {"type": "string", "description": "The task for the agent to perform"},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Optional repo-relative directories to preload "
                                     "(expanded to all files under them)"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Optional specific repo-relative files to preload into the agent's context"},
        },
        "required": ["agent", "prompt"],
    },
}

DISPATCH_WORKERS = {
    "type": "function",
    "name": "dispatch_workers",
    "description": (
        "Create parallel worker agents. Each worker gets ONLY its assigned files "
        "(pre-loaded) and returns its results. Call this once with the full team; you "
        "receive all results when the wave completes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "description": "short label, e.g. 'injection' or 'data-layer'"},
                        "focus": {"type": "string", "description": "what this worker should do"},
                        "paths": {"type": "array", "items": {"type": "string"},
                                  "description": "repo-relative directories this worker owns "
                                                 "(e.g. 'src/auth') — expanded to all files under them. "
                                                 "Prefer this over listing files for large repos."},
                        "files": {"type": "array", "items": {"type": "string"},
                                  "description": "specific repo-relative files this worker owns "
                                                 "(use for individual files; combine with paths)"},
                    },
                    "required": ["role", "focus"],
                },
            }
        },
        "required": ["workers"],
    },
}
READ_FILE = {
    "type": "function", "name": "read_file",
    "description": "Read one repo file (repo-relative path).",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
GREP = {
    "type": "function", "name": "grep",
    "description": "Regex search across the repo (or one file).",
    "parameters": {"type": "object",
                   "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                   "required": ["pattern"]},
}
RUN_SAST = {
    "type": "function", "name": "run_sast",
    "description": ("Run static analysers (bandit/semgrep/ruff/gosec, whichever are installed) "
                    "over a path and return their hits. Read-only — never modifies the repo."),
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string",
                                           "description": "repo-relative file/dir (optional; default whole repo)"}}},
}
CHECK_ADVISORY = {
    "type": "function", "name": "check_advisory",
    "description": "Look up known vulnerabilities for a dependency on OSV.dev.",
    "parameters": {"type": "object",
                   "properties": {"ecosystem": {"type": "string", "description": "e.g. PyPI, npm, Go, crates.io"},
                                  "package": {"type": "string"}, "version": {"type": "string"}},
                   "required": ["ecosystem", "package"]},
}
WEB_SEARCH = {
    "type": "function", "name": "web_search",
    "description": "Targeted web search to ground a SPECIFIC question against docs/advisories. Not for browsing.",
    "parameters": {"type": "object",
                   "properties": {"query": {"type": "string"},
                                  "max_results": {"type": "integer", "description": "default 5"}},
                   "required": ["query"]},
}
READ_PAGE = {
    "type": "function", "name": "read_page",
    "description": "Fetch one web page as text (e.g. an advisory or doc).",
    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
}
SUBMIT_VERDICT = {
    "type": "function", "name": "submit_verdict",
    "description": "Confirm or refute the item you were given. Default is_real=false when uncertain.",
    "parameters": {"type": "object",
                   "properties": {"is_real": {"type": "boolean"}, "confidence": {"type": "number"},
                                  "adjusted_severity": {"type": "string"}, "reasoning": {"type": "string"}},
                   "required": ["is_real", "confidence", "reasoning"]},
}

# Brief-selectable capability tools by name. web_search/read_page are opt-in (search_enabled).
_REGISTRY = {"read_file": READ_FILE, "grep": GREP, "run_sast": RUN_SAST,
             "check_advisory": CHECK_ADVISORY, "web_search": WEB_SEARCH, "read_page": READ_PAGE}
_SEARCH_ONLY = {"web_search", "read_page"}


def submit_results_tool(result_key: str, item_schema: dict, description: str) -> dict:
    """Build the per-brief worker terminal tool. The worker emits a list of result items."""
    return {
        "type": "function", "name": "submit_results", "description": description,
        "parameters": {"type": "object",
                       "properties": {result_key: {"type": "array", "items": item_schema}},
                       "required": [result_key]},
    }


def tools_for(role: str, *, interface="structured", worker_tools=(), verifier_tools=(),
              search_enabled=False, results_tool=None) -> list[dict]:
    """Assemble the flat tool schemas for a role from brief-selected capability names.

    The orchestrator always gets read_file/grep so it can probe the target before
    and between dispatches (deciding *whether/how* to parallelize, not only doing it).
    """
    if role == "orchestrator":
        dispatch = [CREATE_SUBAGENT, ASSIGN_TASK] if interface == "kimi" else [DISPATCH_WORKERS]
        return dispatch + [READ_FILE, GREP]

    def pick(names):
        return [_REGISTRY[n] for n in names
                if n in _REGISTRY and not (n in _SEARCH_ONLY and not search_enabled)]

    if role == "worker":
        return pick(worker_tools) + ([results_tool] if results_tool else [])
    if role == "verifier":
        return pick(verifier_tools) + [SUBMIT_VERDICT]
    raise KeyError(role)


def execute_tool(name: str, arguments: str, *, root: str) -> str:
    """Execute an immediate capability tool; deferred tools return the DEFERRED sentinel."""
    if name in _DEFERRED_NAMES:
        return DEFERRED
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"bad arguments: {exc}"})

    if name == "read_file":
        return json.dumps({"path": args.get("path", ""), "content": repo.read_file(root, args.get("path", ""))})
    if name == "grep":
        return json.dumps({"hits": repo.grep(root, args.get("pattern", ""), args.get("path"))})
    if name == "run_sast":
        return json.dumps(sast.run_sast(root, args.get("path")))
    if name == "check_advisory":
        return json.dumps(advisory.check_advisory(
            args.get("ecosystem", ""), args.get("package", ""), args.get("version")))
    if name == "web_search":
        return json.dumps(search.search(args.get("query", ""), args.get("max_results", 5)))
    if name == "read_page":
        return json.dumps(search.fetch_page(args.get("url", "")))
    return json.dumps({"error": f"unknown tool: {name}"})
