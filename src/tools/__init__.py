"""Flat Open Responses tool schemas + local execution dispatch.

Three tools are *deferred* — handled by the swarm orchestrator, not here:
``dispatch_workers`` (orchestrator), ``report_findings`` (worker terminal),
``submit_verdict`` (verifier terminal). ``read_file``/``grep`` run immediately.
"""

import json

from . import advisory, repo, sast, search

DEFERRED = "__DEFERRED__"
_DEFERRED_NAMES = {"dispatch_workers", "report_findings", "submit_verdict"}

DISPATCH_WORKERS = {
    "type": "function",
    "name": "dispatch_workers",
    "description": (
        "Create parallel worker agents to audit the codebase. Each worker gets "
        "ONLY its assigned files (pre-loaded) and returns its findings. Call this "
        "once with the full team; you receive all findings when the wave completes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "description": "short label, e.g. 'injection' or 'auth'"},
                        "focus": {"type": "string", "description": "what this worker should look for"},
                        "files": {"type": "array", "items": {"type": "string"},
                                  "description": "repo-relative files this worker owns"},
                    },
                    "required": ["role", "focus", "files"],
                },
            }
        },
        "required": ["workers"],
    },
}
READ_FILE = {
    "type": "function",
    "name": "read_file",
    "description": "Read one repo file (repo-relative path). Use to follow an import or read a file outside your slice.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}
GREP = {
    "type": "function",
    "name": "grep",
    "description": "Regex search across the repo (or one file). Use to trace a value to its sink.",
    "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
    },
}
REPORT_FINDINGS = {
    "type": "function",
    "name": "report_findings",
    "description": "Submit your findings and finish. Return findings only — no other commentary.",
    "parameters": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                        "title": {"type": "string"},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "description": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["severity", "title", "file", "description", "confidence"],
                },
            }
        },
        "required": ["findings"],
    },
}
SUBMIT_VERDICT = {
    "type": "function",
    "name": "submit_verdict",
    "description": "Confirm or refute the finding you were given. Default is_real=false when uncertain.",
    "parameters": {
        "type": "object",
        "properties": {
            "is_real": {"type": "boolean"},
            "confidence": {"type": "number"},
            "adjusted_severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
            "reasoning": {"type": "string"},
        },
        "required": ["is_real", "confidence", "reasoning"],
    },
}

RUN_SAST = {
    "type": "function",
    "name": "run_sast",
    "description": (
        "Run static analysers (bandit / semgrep / ruff / gosec, whichever are installed) "
        "over a path and return their hits. Read-only — never modifies the repo. Use it to "
        "back a finding with a real tool hit rather than only your own judgment."
    ),
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string",
                                "description": "repo-relative file or dir (optional; default: whole repo)"}},
    },
}
CHECK_ADVISORY = {
    "type": "function",
    "name": "check_advisory",
    "description": (
        "Look up known vulnerabilities for a dependency on OSV.dev. Use for dependency / "
        "version findings instead of guessing a CVE number."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ecosystem": {"type": "string", "description": "e.g. PyPI, npm, Go, crates.io, Maven"},
            "package": {"type": "string"},
            "version": {"type": "string"},
        },
        "required": ["ecosystem", "package"],
    },
}
WEB_SEARCH = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Targeted web search to ground a SPECIFIC suspected issue against docs/advisories. "
        "Not for open-ended browsing."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"},
                       "max_results": {"type": "integer", "description": "default 5"}},
        "required": ["query"],
    },
}
READ_PAGE = {
    "type": "function",
    "name": "read_page",
    "description": "Fetch one web page as text (e.g. an advisory or doc) to confirm a finding.",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}

# Per-role read-only toolsets. web_search/read_page are opt-in (search_enabled);
# the orchestrator's role/focus steers which tools each worker actually leans on.
_WORKER_TOOLS = [READ_FILE, GREP, RUN_SAST, CHECK_ADVISORY]
_VERIFIER_TOOLS = [CHECK_ADVISORY, RUN_SAST]
_SEARCH_TOOLS = [WEB_SEARCH, READ_PAGE]


def tools_for(role: str, search_enabled: bool = False) -> list[dict]:
    """Flat tool schemas for a swarm role. web_search/read_page only when search_enabled."""
    if role == "orchestrator":
        return [DISPATCH_WORKERS]
    extra = _SEARCH_TOOLS if search_enabled else []
    if role == "worker":
        return _WORKER_TOOLS + extra + [REPORT_FINDINGS]
    if role == "verifier":
        return _VERIFIER_TOOLS + extra + [SUBMIT_VERDICT]
    raise KeyError(role)


def execute_tool(name: str, arguments: str, *, root: str) -> str:
    """Execute an immediate tool; deferred tools return the DEFERRED sentinel.

    Returns a JSON string for immediate tools (read_file/grep).
    """
    if name in _DEFERRED_NAMES:
        return DEFERRED
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"bad arguments: {exc}"})

    if name == "read_file":
        path = args.get("path", "")
        return json.dumps({"path": path, "content": repo.read_file(root, path)})
    if name == "grep":
        hits = repo.grep(root, args.get("pattern", ""), args.get("path"))
        return json.dumps({"hits": hits})
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
