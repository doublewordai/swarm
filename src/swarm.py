"""The audit swarm engine.

One-level swarm: an LLM orchestrator decomposes the audit and dispatches
bounded-context workers (Kimi's "self-designing orchestrator" + "bounded local
context, return only findings"); a flat adversarial verifier stage challenges
each finding (structural anti-groupthink); a final tool-free synthesis turn
writes the report.

Tier behavior lives entirely in ``responses.dispatch``: realtime = blocking
concurrent, async = background submit-then-poll. This engine is tier-agnostic.
"""

import json
import re
from dataclasses import dataclass, field

from . import prompts
from . import responses as R
from . import tools
from .tools import repo

SEV_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@dataclass
class Agent:
    id: str
    role: str  # orchestrator | worker | verifier
    model: str
    input_items: list = field(default_factory=list)
    status: str = "pending"  # pending|in_progress|completed|failed
    rounds: int = 0
    findings: list = field(default_factory=list)
    verdict: dict | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class SwarmConfig:
    model: str
    service_tier: str = "priority"
    background: bool = False
    max_agents: int = 12
    max_files: int = 40
    max_waves: int = 2
    max_rounds: int = 3
    verify: bool = True
    orchestrator_temperature: float = 0.3
    reasoning_effort: str = "minimal"  # minimal|low|medium|high — minimal keeps K2.6 fast


# --- input builders (bounded local context) --------------------------------


def build_worker_input(root: str, role: str, focus: str, files: list[str]) -> list[dict]:
    blocks = []
    for rel in files:
        blocks.append(f"### {rel}\n```\n{repo.read_file(root, rel)}\n```")
    body = "\n\n".join(blocks) if blocks else "(no files assigned)"
    return [
        {"type": "message", "role": "system", "content": prompts.WORKER_SYSTEM},
        {"type": "message", "role": "user",
         "content": f"Role: {role}\nFocus: {focus}\n\nFiles you are auditing:\n\n{body}"},
    ]


def build_verifier_input(root: str, finding: dict) -> list[dict]:
    code = repo.read_file(root, finding.get("file", ""))
    return [
        {"type": "message", "role": "system", "content": prompts.VERIFIER_SYSTEM},
        {"type": "message", "role": "user",
         "content": f"Finding to verify:\n{json.dumps(finding, indent=2)}\n\n"
                    f"Source of {finding.get('file', '?')}:\n```\n{code}\n```"},
    ]


# --- helpers ---------------------------------------------------------------


def _req(model, input_items, tools_=None, temperature=0, max_output_tokens=8192,
         reasoning_effort="minimal"):
    r = {"model": model, "input_items": input_items, "temperature": temperature,
         "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort}
    if tools_:
        r["tools"] = tools_
    return r


def _add_tokens(acc: dict, resp: dict) -> None:
    u = R.usage_of(resp)
    acc["input_tokens"] += u["input_tokens"]
    acc["output_tokens"] += u["output_tokens"]
    acc["reasoning_tokens"] += u.get("reasoning_tokens", 0)


def _echo_outputs(agent: Agent, resp: dict) -> None:
    """Append the model's emitted items back into the agent's input (spec-clean
    continuation without previous_response_id). Preserves the function_call item
    ``id`` when present — strict Responses servers pair it with the output."""
    text = R.text_of(resp)
    if text:
        agent.input_items.append({"type": "message", "role": "assistant", "content": text})
    for item in resp.get("output", []):
        if item.get("type") != "function_call":
            continue
        echoed = {
            "type": "function_call",
            "call_id": item.get("call_id") or item.get("id", ""),
            "name": item.get("name", ""),
            "arguments": item.get("arguments", ""),
        }
        if item.get("id"):
            echoed["id"] = item["id"]
        agent.input_items.append(echoed)


def _tool_output(agent: Agent, call_id: str, output: str) -> None:
    agent.input_items.append({"type": "function_call_output", "call_id": call_id, "output": output})


def dedupe(findings: list[dict]) -> list[dict]:
    """Merge findings sharing (file, normalized title); keep highest severity and
    the max confidence."""
    groups: dict = {}
    for f in findings:
        key = (f.get("file", "").strip(),
               re.sub(r"\s+", " ", f.get("title", "").strip().lower()))
        cur = groups.get(key)
        if cur is None:
            groups[key] = dict(f)
            continue
        conf = max(cur.get("confidence", 0) or 0, f.get("confidence", 0) or 0)
        if SEV_ORDER.get(f.get("severity", "info"), 0) > SEV_ORDER.get(cur.get("severity", "info"), 0):
            merged = dict(f)
        else:
            merged = dict(cur)
        merged["confidence"] = conf
        groups[key] = merged
    return list(groups.values())


# --- phases ----------------------------------------------------------------


def _run_workers(client, root, worker_specs, cfg, tokens, ids, agents, on_event):
    workers = []
    for spec in worker_specs:
        wid = f"worker-{ids['n']}"
        ids["n"] += 1
        w = Agent(id=wid, role="worker", model=cfg.model, status="in_progress",
                  input_items=build_worker_input(root, spec.get("role", "audit"),
                                                  spec.get("focus", ""), spec.get("files", [])),
                  meta={"role": spec.get("role", "audit"), "focus": spec.get("focus", ""),
                        "files": spec.get("files", [])})
        workers.append(w)
        agents.append(w)
    on_event("workers", f"dispatched {len(workers)} worker(s)")

    for _ in range(cfg.max_rounds):
        active = [w for w in workers if w.status == "in_progress"]
        if not active:
            break
        reqs = [_req(cfg.model, w.input_items, tools_=tools.tools_for("worker"),
                     reasoning_effort=cfg.reasoning_effort) for w in active]
        results = R.dispatch(client, reqs, service_tier=cfg.service_tier, background=cfg.background)
        for w, resp in zip(active, results):
            _add_tokens(tokens, resp)
            w.rounds += 1
            if R.finish_of(resp) == "error":
                w.status = "failed"
                continue
            _echo_outputs(w, resp)
            fcs = R.function_calls_of(resp)
            if not fcs:
                w.status = "completed"
                continue
            for fc in fcs:
                if fc["name"] == "report_findings":
                    try:
                        fnds = (json.loads(fc["arguments"] or "{}")).get("findings", [])
                    except json.JSONDecodeError:
                        fnds = []
                    for f in fnds:
                        f.setdefault("found_by", w.meta["role"])
                    w.findings.extend(fnds)
                    _tool_output(w, fc["call_id"], json.dumps({"status": "recorded", "count": len(fnds)}))
                    w.status = "completed"
                else:
                    out = tools.execute_tool(fc["name"], fc["arguments"], root=root)
                    if out == tools.DEFERRED:
                        out = json.dumps({"error": "tool not available to workers"})
                    _tool_output(w, fc["call_id"], out)

    for w in workers:
        if w.status == "in_progress":
            w.status = "completed"  # ran out of rounds; keep whatever findings exist

    findings = []
    for w in workers:
        findings.extend(w.findings)
    return findings


def _run_verifiers(client, root, candidates, cfg, tokens, ids, agents, on_event):
    vagents = []
    reqs = []
    for f in candidates:
        vid = f"verifier-{ids['n']}"
        ids["n"] += 1
        a = Agent(id=vid, role="verifier", model=cfg.model, status="in_progress",
                  input_items=build_verifier_input(root, f), meta={"finding": f})
        vagents.append(a)
        agents.append(a)
        reqs.append(_req(cfg.model, a.input_items, tools_=tools.tools_for("verifier"),
                         reasoning_effort=cfg.reasoning_effort))
    on_event("verify", f"verifying {len(candidates)} finding(s)")
    results = R.dispatch(client, reqs, service_tier=cfg.service_tier, background=cfg.background)
    for a, resp in zip(vagents, results):
        _add_tokens(tokens, resp)
        a.rounds += 1
        verdict = None
        for fc in R.function_calls_of(resp):
            if fc["name"] == "submit_verdict":
                try:
                    verdict = json.loads(fc["arguments"] or "{}")
                except json.JSONDecodeError:
                    verdict = None
        a.verdict = verdict
        a.status = "completed"
    return vagents


# --- top-level orchestration -----------------------------------------------


def run_audit(client, root, files, cfg: SwarmConfig, *, on_event=lambda *_: None) -> dict:
    tokens = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    ids = {"n": 0}
    agents: list[Agent] = []

    repo_map = repo.build_repo_map(root, files)
    orch = Agent(
        id="orchestrator", role="orchestrator", model=cfg.model, status="in_progress",
        input_items=[
            {"type": "message", "role": "system", "content": prompts.ORCHESTRATOR_SYSTEM},
            {"type": "message", "role": "user",
             "content": f"Audit this repository ({len(files)} source files). Use at most "
                        f"{cfg.max_agents} workers.\n\n{repo_map}"},
        ],
    )
    agents.append(orch)

    all_findings: list[dict] = []
    assigned: set[str] = set()
    waves = 0

    while True:
        resp = R.dispatch(client, [_req(cfg.model, orch.input_items,
                                        tools_=tools.tools_for("orchestrator"),
                                        temperature=cfg.orchestrator_temperature,
                                        reasoning_effort=cfg.reasoning_effort)],
                          service_tier=cfg.service_tier, background=cfg.background)[0]
        _add_tokens(tokens, resp)
        orch.rounds += 1
        _echo_outputs(orch, resp)
        dw_calls = [fc for fc in R.function_calls_of(resp) if fc["name"] == "dispatch_workers"]
        if not dw_calls:
            break  # orchestrator finished planning
        fc = dw_calls[0]
        if waves >= cfg.max_waves:
            _tool_output(orch, fc["call_id"], json.dumps({"note": "wave limit reached"}))
            break
        try:
            specs = (json.loads(fc["arguments"] or "{}")).get("workers", [])
        except json.JSONDecodeError:
            specs = []
        if len(specs) > cfg.max_agents:
            on_event("clamp", f"orchestrator asked for {len(specs)} workers; capping at {cfg.max_agents}")
            specs = specs[:cfg.max_agents]
        for s in specs:
            assigned.update(s.get("files", []))
        findings = _run_workers(client, root, specs, cfg, tokens, ids, agents, on_event)
        all_findings.extend(findings)
        _tool_output(orch, fc["call_id"], json.dumps({"findings": findings}))
        waves += 1
        on_event("wave", f"wave {waves} complete: {len(findings)} candidate finding(s)")

    orch.status = "completed"

    candidates = dedupe(all_findings)
    on_event("dedupe", f"{len(all_findings)} raw → {len(candidates)} unique candidate(s)")

    if cfg.verify and candidates:
        vagents = _run_verifiers(client, root, candidates, cfg, tokens, ids, agents, on_event)
        confirmed = []
        for a in vagents:
            f = dict(a.meta["finding"])
            f["verdict"] = a.verdict
            if a.verdict and a.verdict.get("is_real"):
                if a.verdict.get("adjusted_severity"):
                    f["severity"] = a.verdict["adjusted_severity"]
                confirmed.append(f)
    else:
        confirmed = [{**f, "verdict": None} for f in candidates]

    confirmed.sort(key=lambda f: SEV_ORDER.get(f.get("severity", "info"), 0), reverse=True)
    on_event("synthesize", f"writing report from {len(confirmed)} confirmed finding(s)")

    synth_input = [
        {"type": "message", "role": "system", "content": prompts.SYNTHESIS_SYSTEM},
        {"type": "message", "role": "user",
         "content": f"Files audited: {len(files)}. Confirmed findings (JSON):\n"
                    f"{json.dumps(confirmed, indent=2)}\n\nWrite the audit report."},
    ]
    sresp = R.dispatch(client, [_req(cfg.model, synth_input, max_output_tokens=16384,
                                     reasoning_effort=cfg.reasoning_effort)],
                       service_tier=cfg.service_tier, background=cfg.background)[0]
    _add_tokens(tokens, sresp)
    report = R.text_of(sresp) or "# Audit Report\n\n(No report generated.)"

    return {
        "report": report,
        "findings": confirmed,
        "agents": [{"id": a.id, "role": a.role, "status": a.status, "rounds": a.rounds,
                    "meta": a.meta if a.role != "verifier" else {"finding_title":
                        a.meta.get("finding", {}).get("title", "")},
                    "n_findings": len(a.findings)} for a in agents],
        "tokens": tokens,
        "coverage": {"assigned": len(assigned & set(files)), "total": len(files)},
        "waves": waves,
    }
