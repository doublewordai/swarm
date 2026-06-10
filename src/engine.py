"""The generic agent-swarm engine — brief-agnostic.

One-level swarm: an LLM orchestrator decomposes the task and dispatches bounded-context
workers; an optional adversarial verifier stage challenges each result; a synthesis turn
writes the report. WHAT the swarm does is supplied by a `Brief` (prompts, result schema,
per-role tools, dedupe/verify hooks); this module never mentions audits.

Tier behaviour lives entirely in ``responses.dispatch`` (realtime concurrent vs async
background+poll), so the engine is tier-agnostic too.
"""

import json
from dataclasses import dataclass, field

from . import responses as R
from . import tools
from .briefs import Brief
from .tools import repo


@dataclass
class Agent:
    id: str
    role: str  # orchestrator | worker | verifier
    model: str
    input_items: list = field(default_factory=list)
    status: str = "pending"  # pending|in_progress|completed|failed
    rounds: int = 0
    results: list = field(default_factory=list)
    verdict: dict | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class SwarmConfig:
    model: str
    service_tier: str = "priority"
    background: bool = False
    max_agents: int = 100
    max_files: int = 500
    max_waves: int = 2
    max_rounds: int = 3
    verify: bool = True
    orchestrator_temperature: float = 0.3
    reasoning_effort: str = "minimal"
    search_enabled: bool = False


# --- bounded local context builders ----------------------------------------


def build_worker_input(root: str, brief: Brief, role: str, focus: str, files: list[str]) -> list[dict]:
    blocks = [f"### {rel}\n```\n{repo.read_file(root, rel)}\n```" for rel in files]
    body = "\n\n".join(blocks) if blocks else "(no files assigned)"
    return [
        {"type": "message", "role": "system", "content": brief.worker_prompt},
        {"type": "message", "role": "user",
         "content": f"Role: {role}\nFocus: {focus}\n\nFiles you are assigned:\n\n{body}"},
    ]


def build_verifier_input(root: str, brief: Brief, item: dict) -> list[dict]:
    code = repo.read_file(root, item.get("file", "")) if item.get("file") else "(no file)"
    return [
        {"type": "message", "role": "system", "content": brief.verifier_prompt},
        {"type": "message", "role": "user",
         "content": f"Item to verify:\n{json.dumps(item, indent=2)}\n\n"
                    f"Source of {item.get('file', '?')}:\n```\n{code}\n```"},
    ]


def dedupe(items: list[dict], key_fn=None, rank_fn=None) -> list[dict]:
    """Merge items sharing key_fn(item); keep the max rank_fn per key (or first)."""
    if key_fn is None:
        return list(items)
    groups: dict = {}
    for it in items:
        key = key_fn(it)
        cur = groups.get(key)
        if cur is None:
            groups[key] = it
        elif rank_fn is not None and rank_fn(it) > rank_fn(cur):
            groups[key] = it
    return list(groups.values())


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
    text = R.text_of(resp)
    if text:
        agent.input_items.append({"type": "message", "role": "assistant", "content": text})
    for item in resp.get("output", []):
        if item.get("type") != "function_call":
            continue
        echoed = {"type": "function_call",
                  "call_id": item.get("call_id") or item.get("id", ""),
                  "name": item.get("name", ""), "arguments": item.get("arguments", "")}
        if item.get("id"):
            echoed["id"] = item["id"]
        agent.input_items.append(echoed)


def _tool_output(agent: Agent, call_id: str, output: str) -> None:
    agent.input_items.append({"type": "function_call_output", "call_id": call_id, "output": output})


# --- phases ----------------------------------------------------------------


def _run_workers(client, root, brief, worker_specs, cfg, tokens, ids, agents, on_event, results_tool):
    workers = []
    for spec in worker_specs:
        w = Agent(id=f"worker-{ids['n']}", role="worker", model=cfg.model, status="in_progress",
                  input_items=build_worker_input(root, brief, spec.get("role", "worker"),
                                                  spec.get("focus", ""), spec.get("files", [])),
                  meta={"role": spec.get("role", "worker"), "focus": spec.get("focus", ""),
                        "files": spec.get("files", [])})
        ids["n"] += 1
        workers.append(w)
        agents.append(w)
    on_event("workers", f"dispatched {len(workers)} worker(s)")

    wtools = tools.tools_for("worker", worker_tools=brief.worker_tools,
                             search_enabled=cfg.search_enabled, results_tool=results_tool)
    for _ in range(cfg.max_rounds):
        active = [w for w in workers if w.status == "in_progress"]
        if not active:
            break
        reqs = [_req(cfg.model, w.input_items, tools_=wtools, reasoning_effort=cfg.reasoning_effort)
                for w in active]
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
                if fc["name"] == "submit_results":
                    try:
                        items = (json.loads(fc["arguments"] or "{}")).get(brief.result_key, [])
                    except json.JSONDecodeError:
                        items = []
                    for it in items:
                        it.setdefault("found_by", w.meta["role"])
                    w.results.extend(items)
                    _tool_output(w, fc["call_id"], json.dumps({"status": "recorded", "count": len(items)}))
                    w.status = "completed"
                else:
                    out = tools.execute_tool(fc["name"], fc["arguments"], root=root)
                    if out == tools.DEFERRED:
                        out = json.dumps({"error": "tool not available here"})
                    _tool_output(w, fc["call_id"], out)

    for w in workers:
        if w.status == "in_progress":
            w.status = "completed"
    out = []
    for w in workers:
        out.extend(w.results)
    return out


def _run_verifiers(client, root, brief, candidates, cfg, tokens, ids, agents, on_event):
    vtools = tools.tools_for("verifier", verifier_tools=brief.verifier_tools,
                             search_enabled=cfg.search_enabled)
    vagents, reqs = [], []
    for item in candidates:
        a = Agent(id=f"verifier-{ids['n']}", role="verifier", model=cfg.model, status="in_progress",
                  input_items=build_verifier_input(root, brief, item), meta={"item": item})
        ids["n"] += 1
        vagents.append(a)
        agents.append(a)
        reqs.append(_req(cfg.model, a.input_items, tools_=vtools, reasoning_effort=cfg.reasoning_effort))
    on_event("verify", f"verifying {len(candidates)} item(s)")
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


def run_swarm(client, brief: Brief, root, files, cfg: SwarmConfig, *, on_event=lambda *_: None) -> dict:
    tokens = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    ids = {"n": 0}
    agents: list[Agent] = []
    results_tool = tools.submit_results_tool(brief.result_key, brief.result_schema, brief.submit_description)

    repo_map = repo.build_repo_map(root, files)
    orch = Agent(id="orchestrator", role="orchestrator", model=cfg.model, status="in_progress",
                 input_items=[
                     {"type": "message", "role": "system", "content": brief.orchestrator_prompt},
                     {"type": "message", "role": "user",
                      "content": f"Work over this repository ({len(files)} source files). Use at most "
                                 f"{cfg.max_agents} workers.\n\n{repo_map}"},
                 ])
    agents.append(orch)

    all_results: list[dict] = []
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
        dw = [fc for fc in R.function_calls_of(resp) if fc["name"] == "dispatch_workers"]
        if not dw:
            break
        fc = dw[0]
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
        wave_results = _run_workers(client, root, brief, specs, cfg, tokens, ids, agents, on_event, results_tool)
        all_results.extend(wave_results)
        _tool_output(orch, fc["call_id"], json.dumps({brief.result_key: wave_results}))
        waves += 1
        on_event("wave", f"wave {waves} complete: {len(wave_results)} item(s)")

    orch.status = "completed"

    candidates = dedupe(all_results, brief.dedupe_key, brief.rank)
    on_event("dedupe", f"{len(all_results)} raw → {len(candidates)} unique")

    if cfg.verify and brief.verifier_prompt and candidates:
        vagents = _run_verifiers(client, root, brief, candidates, cfg, tokens, ids, agents, on_event)
        confirmed = []
        for a in vagents:
            it = dict(a.meta["item"])
            it["verdict"] = a.verdict
            if a.verdict and a.verdict.get("is_real"):
                if a.verdict.get("adjusted_severity"):
                    it["severity"] = a.verdict["adjusted_severity"]
                confirmed.append(it)
    else:
        confirmed = [{**c, "verdict": None} for c in candidates]

    if brief.rank:
        confirmed.sort(key=brief.rank, reverse=True)
    on_event("synthesize", f"writing report from {len(confirmed)} item(s)")

    synth_input = [
        {"type": "message", "role": "system", "content": brief.synthesis_prompt},
        {"type": "message", "role": "user",
         "content": f"Files covered: {len(files)}. Results (JSON):\n{json.dumps(confirmed, indent=2)}\n\n"
                    f"Write the report."},
    ]
    sresp = R.dispatch(client, [_req(cfg.model, synth_input, max_output_tokens=16384,
                                     reasoning_effort=cfg.reasoning_effort)],
                       service_tier=cfg.service_tier, background=cfg.background)[0]
    _add_tokens(tokens, sresp)
    report = R.text_of(sresp) or "# Report\n\n(No report generated.)"

    return {
        "report": report,
        "result_key": brief.result_key,
        "results": confirmed,
        "agents": [{"id": a.id, "role": a.role, "status": a.status, "rounds": a.rounds,
                    "meta": a.meta if a.role != "verifier" else {"item": a.meta.get("item", {}).get("title", "")},
                    "n_results": len(a.results)} for a in agents],
        "tokens": tokens,
        "coverage": {"assigned": len(assigned & set(files)), "total": len(files)},
        "waves": waves,
    }
