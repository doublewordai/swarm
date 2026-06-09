"""Read-only static analysis: run on-PATH analyzers over the target and parse hits.

These analyzers *read and report* — they never modify the repo (v1 is non-mutating).
A finding can then be backed by a real tool hit, not just the model's say-so. Whatever
analyzer is installed runs; missing ones are skipped. Install some with
``uv sync --extra sast`` (bandit), or have semgrep/ruff/gosec on PATH.
"""

import json
import shutil
import subprocess

_PY_ANALYZERS = ("bandit", "semgrep", "ruff")


def available_analyzers() -> list[str]:
    """Names of supported analyzers found on PATH."""
    found = [t for t in _PY_ANALYZERS if shutil.which(t)]
    if shutil.which("gosec"):
        found.append("gosec")
    return found


def _run(cmd: list[str], cwd: str, timeout: int) -> str:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout
    except Exception:  # noqa: BLE001 - missing tool / timeout → no hits
        return ""


def _parse_bandit(stdout: str) -> list[dict]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    return [{
        "tool": "bandit", "file": r.get("filename"), "line": r.get("line_number"),
        "rule": r.get("test_id"), "severity": (r.get("issue_severity") or "").lower(),
        "message": r.get("issue_text"),
    } for r in data.get("results", [])]


def _parse_ruff(stdout: str) -> list[dict]:
    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for r in data:
        loc = r.get("location") or {}
        out.append({"tool": "ruff", "file": r.get("filename"), "line": loc.get("row"),
                    "rule": r.get("code"), "severity": "low", "message": r.get("message")})
    return out


def _parse_semgrep(stdout: str) -> list[dict]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for r in data.get("results", []):
        start = r.get("start") or {}
        extra = r.get("extra") or {}
        out.append({"tool": "semgrep", "file": r.get("path"), "line": start.get("line"),
                    "rule": r.get("check_id"), "severity": (extra.get("severity") or "").lower(),
                    "message": extra.get("message")})
    return out


def run_sast(root: str, path: str | None = None, max_hits: int = 60, timeout: int = 90) -> dict:
    """Run available analyzers over ``path`` (default: whole repo) and return hits.

    Returns {ran: [analyzer], hits: [{tool,file,line,rule,severity,message}], truncated, note}.
    """
    analyzers = available_analyzers()
    if not analyzers:
        return {"ran": [], "hits": [],
                "note": "no SAST analyzers on PATH (try `uv sync --extra sast`, "
                        "or install semgrep/ruff/gosec)"}
    target = path or "."
    ran: list[str] = []
    hits: list[dict] = []
    if "bandit" in analyzers:
        hits += _parse_bandit(_run(["bandit", "-r", target, "-f", "json", "-q"], root, timeout))
        ran.append("bandit")
    if "ruff" in analyzers:
        hits += _parse_ruff(_run(["ruff", "check", target, "--output-format", "json"], root, timeout))
        ran.append("ruff")
    if "semgrep" in analyzers:
        hits += _parse_semgrep(
            _run(["semgrep", "--config", "auto", "--json", "--quiet", target], root, timeout))
        ran.append("semgrep")
    return {"ran": ran, "hits": hits[:max_hits], "truncated": len(hits) > max_hits}
