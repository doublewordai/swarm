"""Flat Open Responses tool schemas + local execution dispatch.

Three tools are *deferred* — handled by the swarm orchestrator, not here:
``dispatch_workers`` (orchestrator), ``report_findings`` (worker terminal),
``submit_verdict`` (verifier terminal). ``read_file``/``grep`` run immediately.
"""

import json

from . import repo

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

ORCHESTRATOR_TOOLS = [DISPATCH_WORKERS]
WORKER_TOOLS = [READ_FILE, GREP, REPORT_FINDINGS]
VERIFIER_TOOLS = [SUBMIT_VERDICT]

_BY_ROLE = {
    "orchestrator": ORCHESTRATOR_TOOLS,
    "worker": WORKER_TOOLS,
    "verifier": VERIFIER_TOOLS,
}


def tools_for(role: str) -> list[dict]:
    """Return the flat tool schemas for a swarm role."""
    return _BY_ROLE[role]


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
    return json.dumps({"error": f"unknown tool: {name}"})
