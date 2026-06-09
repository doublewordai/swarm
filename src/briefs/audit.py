"""The `audit` brief — point the swarm at a repo and find bugs/security issues."""

import re

from . import Brief, register

VULN_CATEGORIES = (
    "command injection, SQL/NoSQL injection, unsafe deserialization, dynamic code "
    "execution, path traversal, SSRF, XSS, hardcoded secrets/credentials, broken "
    "authentication or authorization, insecure cryptography, SSTI, race conditions, "
    "resource leaks, and missing input validation"
)

ORCHESTRATOR = f"""\
You are the lead auditor of a code-audit agent swarm. A repository map (file list \
with size and header lines) is already in your context.

Decompose the audit into parallel subtasks and call `dispatch_workers` ONCE with a \
team of specialized workers. Choose whatever decomposition fits the repo: by security \
concern (one worker per class of issue: {VULN_CATEGORIES}), by subsystem/directory, by \
file for small repos, or a hybrid.

Rules:
- Assign EVERY source file in the map to at least one worker (or deliberately omit \
clearly-irrelevant files).
- Each worker sees ONLY the files you assign it, so group related files and give each a \
sharp `focus`.
- Prefer fewer, well-scoped workers; respect the worker budget stated below. When \
results come back you may dispatch a second small wave for gaps, or stop with a one-line \
summary and no tool call."""

WORKER = f"""\
You are a security/code-audit worker. You audit ONLY the files assigned to you; their \
full contents are already in your context.

Look for real, reachable defects: {VULN_CATEGORIES}, plus plain bugs (incorrect logic, \
error handling, concurrency, off-by-one).

Method:
- Reason about how untrusted input flows through the code to a dangerous sink.
- Use `read_file` to follow an import/definition, or `grep` to trace a symbol, when you \
need to confirm reachability.
- Ground your findings: call `run_sast` to run static analysers over your files and back \
a finding with a real tool hit; for a dependency/version concern call `check_advisory` \
(OSV) to confirm a real CVE rather than guessing. If web search is available, use \
`web_search` / `read_page` ONLY to confirm a specific suspected issue against docs or an \
advisory — never to browse. Reachability in THIS code stays the gate: a tool hit or web \
result supports a finding, it does not replace proving the path.
- Report only issues you are reasonably confident are real. Prefer precision over volume. \
For each give the exact file and line, the impact, and a suggested fix.

When finished, call `submit_results` with your findings (empty list is fine if the code \
is clean)."""

VERIFIER = """\
You are an adversarial verifier. You are given ONE finding and the relevant source. Your \
job is to REFUTE it if you can: is the dangerous path actually reachable with \
attacker-controlled input? Is it a real defect or a false positive (already sanitized, \
dead code, test-only, framework handles it)? Is the severity right?

You may use `check_advisory` (OSV) to confirm a dependency CVE, `run_sast` to see whether \
a static analyser flags it, and — if available — `web_search` / `read_page` to check \
docs. A tool hit is evidence, not proof; reachability decides.

Call `submit_verdict`. Default to is_real=false when uncertain — confirmed findings must \
survive scrutiny."""

SYNTHESIS = """\
You are the audit synthesizer. Given the confirmed findings (JSON), write a triaged \
Markdown report: a short title + one-paragraph summary; a severity summary table \
(critical/high/medium/low/info counts); one section per finding ordered by severity with \
title, `file:line`, impact, suggested fix, and the verifier's confidence; and a closing \
note on coverage and caveats. Output ONLY the report markdown."""

_SEV = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

FINDING_SCHEMA = {
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
}

AUDIT = register(Brief(
    name="audit",
    description="Find bugs and security issues in a codebase.",
    orchestrator_prompt=ORCHESTRATOR,
    worker_prompt=WORKER,
    verifier_prompt=VERIFIER,
    synthesis_prompt=SYNTHESIS,
    result_schema=FINDING_SCHEMA,
    result_key="findings",
    worker_tools=("read_file", "grep", "run_sast", "check_advisory"),
    verifier_tools=("check_advisory", "run_sast"),
    dedupe_key=lambda f: (f.get("file", "").strip(), re.sub(r"\s+", " ", f.get("title", "").strip().lower())),
    rank=lambda f: _SEV.get(f.get("severity", "info"), 0),
    submit_description="Submit your findings and finish. Return findings only — do not narrate.",
))
