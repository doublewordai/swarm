"""The generic agent-swarm engine — brief-agnostic.

One-level swarm: an LLM orchestrator decomposes the task and dispatches bounded-context
workers; an optional adversarial verifier stage challenges each result; a synthesis turn
writes the report. WHAT the swarm does is supplied by a `Brief` (prompts, result schema,
per-role tools, dedupe/verify hooks); this module never mentions audits.

Two orchestration interfaces (``cfg.interface``):

- ``structured`` (default): the orchestrator calls ``dispatch_workers`` with
  ``{role, focus, files}`` specs; the harness builds each worker's bounded context.
- ``kimi``: the interface Kimi K2.5/K2.6 were RL-trained on (K2.5 tech report,
  Appendix E.8): ``create_subagent(name, system_prompt)`` registers reusable agent
  configs whose system prompts the orchestrator itself authors, and
  ``assign_task(agent, prompt[, files])`` dispatches tasks to them incrementally.
  Parallel ``assign_task`` calls in one turn run concurrently.

Either way the downstream pipeline is identical: dedupe → verify (vote panel) →
synthesize. Failure is loud: a dead orchestrator or a workerless run raises
``SwarmError`` instead of shipping a vacuous report.

Tier behaviour lives entirely in ``responses.dispatch`` (realtime concurrent vs async
background+poll), so the engine is tier-agnostic too.
"""

import json
from dataclasses import dataclass, field

import jsonschema

from . import responses as R
from . import tools
from .briefs import Brief
from .tools import repo


class SwarmError(RuntimeError):
    """Fatal swarm failure — the run produced nothing usable and must not look like success."""


@dataclass
class Agent:
    id: str
    role: str  # orchestrator | worker | verifier
    model: str
    input_items: list = field(default_factory=list)
    status: str = "pending"  # pending|in_progress|completed|no_submit|failed
    rounds: int = 0
    results: list = field(default_factory=list)
    verdict: dict | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class SwarmConfig:
    model: str
    worker_model: str | None = None      # model for workers/verifiers (None ⇒ model)
    interface: str = "structured"        # structured | kimi (see module docstring)
    service_tier: str = "priority"
    background: bool = False
    max_agents: int = 100                # total worker budget across the run
    max_files: int = 500
    max_waves: int = 2                   # structured: dispatch_workers waves
    max_steps: int = 8                   # orchestrator turns (either interface)
    max_rounds: int = 3                  # tool rounds per worker/verifier (+1 forced submit)
    max_files_per_worker: int = 30       # oversized specs are split engine-side
    worker_context_chars: int = 200_000  # preload budget per worker (~50k tokens)
    map_max_chars: int = 200_000         # repo-map budget for the orchestrator
    verify: bool = True
    verify_votes: int = 1                # verifiers per item; majority decides
    orchestrator_temperature: float = 0.3
    reasoning_effort: str = "minimal"
    search_enabled: bool = False
    max_concurrent: int = 12             # parallel in-flight requests per dispatch

    def agent_model(self) -> str:
        return self.worker_model or self.model


# --- bounded local context builders ----------------------------------------


def _files_body(root: str, files: list[str], budget_chars: int) -> str:
    """Preload file contents up to the budget; list the rest as fetchable."""
    blocks, deferred, used = [], [], 0
    for rel in files:
        block = f"### {rel}\n```\n{repo.read_file(root, rel)}\n```"
        if blocks and used + len(block) > budget_chars:
            deferred.append(rel)
            continue
        blocks.append(block)  # always preload at least one file (read_file caps size)
        used += len(block)
    body = "\n\n".join(blocks) if blocks else "(no files assigned)"
    if deferred:
        body += ("\n\nAssigned but NOT preloaded (context budget) — fetch with `read_file` "
                 "as needed:\n" + "\n".join(f"- {rel}" for rel in deferred))
    return body


def build_worker_input(root: str, brief: Brief, role: str, focus: str, files: list[str],
                       *, budget_chars: int = 200_000, system_prompt: str | None = None,
                       task: str | None = None) -> list[dict]:
    """Bounded local context for one worker.

    Structured interface: brief-authored system prompt + role/focus/file slice.
    Kimi interface: orchestrator-authored ``system_prompt`` + free-text ``task``.
    """
    body = _files_body(root, files, budget_chars)
    system = system_prompt if system_prompt is not None else brief.worker_prompt
    if task is not None:
        user = task + (f"\n\nFiles preloaded for you:\n\n{body}" if files else "")
    else:
        user = f"Role: {role}\nFocus: {focus}\n\nFiles you are assigned:\n\n{body}"
    return [
        {"type": "message", "role": "system", "content": system},
        {"type": "message", "role": "user", "content": user},
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


def _validate_items(raw, schema: dict) -> tuple[list[dict], int]:
    """Keep only schema-valid dict items; return (valid, n_dropped)."""
    if not isinstance(raw, list):
        return [], 0 if raw in (None, [], {}) else 1
    valid, dropped = [], 0
    for it in raw:
        if not isinstance(it, dict):
            dropped += 1
            continue
        try:
            jsonschema.validate(it, schema)
            valid.append(it)
        except jsonschema.ValidationError:
            dropped += 1
    return valid, dropped


def _split_specs(specs: list[dict], max_files: int) -> list[dict]:
    """Split oversized worker specs so no worker owns more than max_files files."""
    out = []
    for s in specs:
        files = s.get("files") or []
        if len(files) <= max_files:
            out.append(s)
            continue
        chunks = [files[i:i + max_files] for i in range(0, len(files), max_files)]
        for j, chunk in enumerate(chunks, 1):
            out.append({**s, "role": f"{s.get('role', 'worker')} (part {j}/{len(chunks)})",
                        "files": chunk})
    return out


# --- helpers ---------------------------------------------------------------


def _req(model, input_items, tools_=None, temperature=0, max_output_tokens=8192,
         reasoning_effort="minimal", tool_choice=None):
    r = {"model": model, "input_items": input_items, "temperature": temperature,
         "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort}
    if tools_:
        r["tools"] = tools_
    if tool_choice:
        r["tool_choice"] = tool_choice
    return r


def _dispatch(client, reqs, cfg: SwarmConfig) -> list[dict]:
    return R.dispatch(client, reqs, service_tier=cfg.service_tier,
                      background=cfg.background, max_concurrent=cfg.max_concurrent)


def _new_tokens() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "by_model": {}}


def _add_tokens(acc: dict, resp: dict, model: str) -> None:
    u = R.usage_of(resp)
    per = acc["by_model"].setdefault(
        model, {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0})
    for k in ("input_tokens", "output_tokens", "reasoning_tokens"):
        acc[k] += u.get(k, 0)
        per[k] += u.get(k, 0)


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


def _execute_capability(fc: dict, root: str) -> str:
    out = tools.execute_tool(fc["name"], fc["arguments"], root=root)
    if out == tools.DEFERRED:
        out = json.dumps({"error": "tool not available here"})
    return out


# --- agent pool (shared by workers and verifiers) ----------------------------

_FORCE_MSG = ("Your tool budget is exhausted. Call `{name}` NOW with everything you have "
              "so far. An empty result set is acceptable; losing your work is not.")


def _run_pool(client, pool: list[Agent], tools_: list[dict], terminal: str, on_terminal,
              cfg: SwarmConfig, tokens: dict, *, root: str) -> None:
    """Drive agents in lockstep tool rounds until each calls the terminal tool.

    Capability tool calls are executed and answered; agents that exhaust
    ``cfg.max_rounds`` (or stop emitting tool calls) get one final turn with
    ``tool_choice`` pinned to the terminal tool, so work is submitted rather than
    silently lost. Agents that still don't submit end as ``no_submit``.
    """

    def consume(agent: Agent, resp: dict, forced: bool = False) -> None:
        _add_tokens(tokens, resp, agent.model)
        agent.rounds += 1
        if R.finish_of(resp) == "error":
            agent.status = "failed"
            agent.meta["error"] = resp.get("_error", "request failed")
            return
        _echo_outputs(agent, resp)
        fcs = R.function_calls_of(resp)
        if not fcs:
            agent.meta["stopped"] = True  # stopped talking without submitting → forced turn
            return
        for fc in fcs:
            if fc["name"] == terminal:
                _tool_output(agent, fc["call_id"], on_terminal(agent, fc))
                agent.status = "completed"
            elif forced:
                _tool_output(agent, fc["call_id"],
                             json.dumps({"error": f"only {terminal} is accepted now"}))
            else:
                _tool_output(agent, fc["call_id"], _execute_capability(fc, root))

    for _ in range(cfg.max_rounds):
        active = [a for a in pool if a.status == "in_progress" and not a.meta.get("stopped")]
        if not active:
            break
        reqs = [_req(a.model, a.input_items, tools_=tools_,
                     reasoning_effort=cfg.reasoning_effort) for a in active]
        for a, resp in zip(active, _dispatch(client, reqs, cfg)):
            consume(a, resp)

    stragglers = [a for a in pool if a.status == "in_progress"]
    if stragglers:
        for a in stragglers:
            a.input_items.append({"type": "message", "role": "user",
                                  "content": _FORCE_MSG.format(name=terminal)})
        reqs = [_req(a.model, a.input_items, tools_=tools_,
                     tool_choice={"type": "function", "name": terminal},
                     reasoning_effort=cfg.reasoning_effort) for a in stragglers]
        for a, resp in zip(stragglers, _dispatch(client, reqs, cfg)):
            consume(a, resp, forced=True)
    for a in pool:
        if a.status == "in_progress":
            a.status = "no_submit"


# --- workers -----------------------------------------------------------------


def _run_workers(client, root, brief, specs, cfg, tokens, ids, agents, on_event,
                 results_tool) -> list[Agent]:
    """Spawn one worker per spec, run their tool loops, return the worker agents.

    A spec is {role, focus, files} (structured) optionally carrying
    ``_system_prompt``/``_task``/``_fc`` (kimi interface).
    """
    workers = []
    for spec in specs:
        files = spec.get("files") or []
        w = Agent(
            id=f"worker-{ids['n']}", role="worker", model=cfg.agent_model(),
            status="in_progress",
            input_items=build_worker_input(
                root, brief, spec.get("role", "worker"), spec.get("focus", ""), files,
                budget_chars=cfg.worker_context_chars,
                system_prompt=spec.get("_system_prompt"), task=spec.get("_task")),
            meta={"role": spec.get("role", "worker"), "focus": spec.get("focus", ""),
                  "files": files, **({"fc_id": spec["_fc"]} if "_fc" in spec else {})})
        ids["n"] += 1
        workers.append(w)
        agents.append(w)
    on_event("workers", f"dispatched {len(workers)} worker(s)")

    invalid = {"n": 0}

    def on_submit(agent: Agent, fc: dict) -> str:
        try:
            raw = (json.loads(fc["arguments"] or "{}")).get(brief.result_key, [])
        except json.JSONDecodeError:
            raw = []
        items, dropped = _validate_items(raw, brief.result_schema)
        invalid["n"] += dropped
        for it in items:
            it.setdefault("found_by", agent.meta["role"])
        agent.results.extend(items)
        return json.dumps({"status": "recorded", "count": len(items),
                           "invalid_dropped": dropped})

    wtools = tools.tools_for("worker", worker_tools=brief.worker_tools,
                             search_enabled=cfg.search_enabled, results_tool=results_tool)
    _run_pool(client, workers, wtools, "submit_results", on_submit, cfg, tokens, root=root)
    if invalid["n"]:
        on_event("invalid", f"dropped {invalid['n']} schema-invalid item(s) "
                            f"(see {brief.result_key} schema)")
    return workers


def _wave_feedback(result_key: str, workers: list[Agent], max_listed: int = 50) -> dict:
    """What the orchestrator sees after a wave: results + per-worker status + gaps."""
    results = [r for w in workers for r in w.results]
    unreported = sorted({f for w in workers if w.status != "completed"
                         for f in w.meta.get("files", [])})
    listed = unreported[:max_listed]
    if len(unreported) > max_listed:
        listed.append(f"... and {len(unreported) - max_listed} more")
    return {
        result_key: results,
        "workers": [{"id": w.id, "role": w.meta.get("role", w.id), "status": w.status,
                     "n_results": len(w.results)} for w in workers],
        "files_unreported": listed,
    }


def _count_workers(agents: list[Agent]) -> int:
    return sum(1 for a in agents if a.role == "worker")


def _track_wave_steps(steps: dict, workers: list[Agent]) -> None:
    steps["critical"] += max((w.rounds for w in workers), default=0)
    steps["total"] += sum(w.rounds for w in workers)


# --- orchestration: structured interface --------------------------------------


_STRUCTURED_NOTE = """

# Dispatch interface
Call `dispatch_workers` with your team: [{role, focus, files}] — each worker sees ONLY \
its assigned files. Call it once with the full team; after results return (with \
per-worker status and any unreported files) you may dispatch ONE smaller gap-filling \
wave. You may use `read_file`/`grep` yourself to probe the repo before deciding."""

_KIMI_NOTE = """

# Dispatch interface
Design your own team. Call `create_subagent(name, system_prompt)` to define each \
specialist — you author its system prompt — then `assign_task(agent, prompt, files?)` \
to dispatch subtasks. Multiple assign_task calls in ONE turn run in PARALLEL; pass \
`files` (repo-relative) to preload code into that agent's context. Agents are reusable \
across tasks. You may use `read_file`/`grep` yourself to probe the repo first."""


def _orchestrator_agent(brief: Brief, cfg: SwarmConfig, repo_map: str, n_files: int) -> Agent:
    note = _KIMI_NOTE if cfg.interface == "kimi" else _STRUCTURED_NOTE
    return Agent(
        id="orchestrator", role="orchestrator", model=cfg.model, status="in_progress",
        input_items=[
            {"type": "message", "role": "system", "content": brief.orchestrator_prompt + note},
            {"type": "message", "role": "user",
             "content": f"Work over this repository ({n_files} source files). Use at most "
                        f"{cfg.max_agents} workers total.\n\n{repo_map}"},
        ])


def _orch_turn(client, orch: Agent, otools, cfg, tokens, steps) -> dict:
    resp = _dispatch(client, [_req(cfg.model, orch.input_items, tools_=otools,
                                   temperature=cfg.orchestrator_temperature,
                                   reasoning_effort=cfg.reasoning_effort)], cfg)[0]
    _add_tokens(tokens, resp, cfg.model)
    orch.rounds += 1
    steps["critical"] += 1
    steps["total"] += 1
    if R.finish_of(resp) == "error":
        raise SwarmError(f"orchestrator call failed: {resp.get('_error', 'request failed')} "
                         "— aborting rather than writing an empty report")
    _echo_outputs(orch, resp)
    return resp


def _orchestrate_structured(client, root, brief, files, cfg, tokens, steps, ids, agents,
                            on_event, results_tool, orch: Agent):
    all_results: list[dict] = []
    assigned: set[str] = set()
    waves = 0
    otools = tools.tools_for("orchestrator")

    for _ in range(cfg.max_steps):
        resp = _orch_turn(client, orch, otools, cfg, tokens, steps)
        fcs = R.function_calls_of(resp)
        if not fcs:
            orch.meta["closing_note"] = R.text_of(resp)
            break
        for fc in fcs:
            if fc["name"] != "dispatch_workers":
                _tool_output(orch, fc["call_id"], _execute_capability(fc, root))
                continue
            if waves >= cfg.max_waves:
                _tool_output(orch, fc["call_id"], json.dumps(
                    {"note": "wave limit reached — no more workers; summarize and stop"}))
                continue
            try:
                specs = (json.loads(fc["arguments"] or "{}")).get("workers", [])
            except json.JSONDecodeError:
                specs = []
            specs = _split_specs(specs, cfg.max_files_per_worker)
            budget = cfg.max_agents - _count_workers(agents)
            if len(specs) > budget:
                on_event("clamp", f"orchestrator asked for {len(specs)} workers; "
                                  f"capping at remaining budget {budget}")
                specs = specs[:budget]
            for s in specs:
                assigned.update(s.get("files") or [])
            workers = _run_workers(client, root, brief, specs, cfg, tokens, ids, agents,
                                   on_event, results_tool)
            _track_wave_steps(steps, workers)
            wave_results = [r for w in workers for r in w.results]
            all_results.extend(wave_results)
            waves += 1
            on_event("wave", f"wave {waves} complete: {len(wave_results)} item(s)")
            _tool_output(orch, fc["call_id"], json.dumps(_wave_feedback(brief.result_key, workers)))

    orch.status = "completed"
    return all_results, assigned, waves


# --- orchestration: kimi interface ---------------------------------------------


def _kimi_contract(brief: Brief) -> str:
    return ("\n\n---\nHARNESS CONTRACT: investigate with your tools, then FINISH by "
            f"calling `submit_results` with your items under `{brief.result_key}` "
            f"(schema-enforced). {brief.submit_description}")


def _orchestrate_kimi(client, root, brief, files, cfg, tokens, steps, ids, agents,
                      on_event, results_tool, orch: Agent):
    all_results: list[dict] = []
    assigned: set[str] = set()
    waves = 0
    subagents: dict[str, str] = {}
    otools = tools.tools_for("orchestrator", interface="kimi")

    for _ in range(cfg.max_steps):
        resp = _orch_turn(client, orch, otools, cfg, tokens, steps)
        fcs = R.function_calls_of(resp)
        if not fcs:
            orch.meta["closing_note"] = R.text_of(resp)
            break

        task_fcs = []
        for fc in fcs:
            if fc["name"] == "create_subagent":
                try:
                    args = json.loads(fc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                name = (args.get("name") or f"agent-{len(subagents)}").strip()
                subagents[name] = args.get("system_prompt", "")
                _tool_output(orch, fc["call_id"], json.dumps({"status": "created", "agent": name}))
            elif fc["name"] == "assign_task":
                task_fcs.append(fc)
            else:
                _tool_output(orch, fc["call_id"], _execute_capability(fc, root))

        specs, spec_fcs = [], []
        for fc in task_fcs:
            try:
                args = json.loads(fc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            name = (args.get("agent") or "").strip()
            if name not in subagents:
                _tool_output(orch, fc["call_id"], json.dumps(
                    {"error": f"unknown agent: {name!r} — create it with create_subagent first"}))
                continue
            if _count_workers(agents) + len(specs) >= cfg.max_agents:
                _tool_output(orch, fc["call_id"], json.dumps(
                    {"error": f"agent budget exhausted ({cfg.max_agents}) — synthesize with what you have"}))
                continue
            task_files = args.get("files") or []
            specs.append({"role": name, "focus": (args.get("prompt") or "")[:120],
                          "files": task_files, "_fc": fc["call_id"],
                          "_system_prompt": subagents[name] + _kimi_contract(brief),
                          "_task": args.get("prompt", "")})
            spec_fcs.append(fc)
            assigned.update(task_files)

        if specs:
            specs = _split_specs(specs, cfg.max_files_per_worker)
            workers = _run_workers(client, root, brief, specs, cfg, tokens, ids, agents,
                                   on_event, results_tool)
            _track_wave_steps(steps, workers)
            waves += 1
            for fc in spec_fcs:
                ws = [w for w in workers if w.meta.get("fc_id") == fc["call_id"]]
                items = [r for w in ws for r in w.results]
                all_results.extend(items)
                _tool_output(orch, fc["call_id"], json.dumps({
                    "agent": ws[0].meta["role"] if ws else "?",
                    "status": ("completed" if ws and all(w.status == "completed" for w in ws)
                               else (ws[0].status if ws else "failed")),
                    "n_results": len(items),
                    brief.result_key: items,
                }))
            on_event("wave", f"turn dispatched {len(specs)} task(s): "
                             f"{sum(len(w.results) for w in workers)} item(s)")

    orch.status = "completed"
    return all_results, assigned, waves


# --- verification --------------------------------------------------------------


def _aggregate_votes(group: list[Agent], single_vote: bool) -> tuple[str, dict | None]:
    """Majority over cast verdicts → confirmed/refuted; ties or none → unverified."""
    verdicts = [a.verdict for a in group if a.verdict is not None]
    yes = sum(1 for v in verdicts if v.get("is_real"))
    no = len(verdicts) - yes
    if not verdicts or yes == no:
        return "unverified", (None if single_vote else {"is_real": None, "votes": verdicts})
    is_real = yes > no
    if single_vote:
        return ("confirmed" if is_real else "refuted"), verdicts[0]
    return ("confirmed" if is_real else "refuted"), {"is_real": is_real, "votes": verdicts}


def _adjusted_severity(verdict: dict | None) -> str | None:
    if not verdict:
        return None
    for v in verdict.get("votes", [verdict]):
        if v.get("is_real") and v.get("adjusted_severity"):
            return v["adjusted_severity"]
    return None


def _run_verifiers(client, root, brief, candidates, cfg, tokens, steps, ids, agents,
                   on_event) -> list[tuple[dict, str, dict | None]]:
    """Verify each candidate with a panel of cfg.verify_votes independent skeptics.

    Verifiers get a real tool loop (same budget as workers) and a forced
    submit_verdict turn — an investigating verifier is never silently discarded.
    Returns [(item, verification, verdict)] with verification in
    confirmed|refuted|unverified.
    """
    vtools = tools.tools_for("verifier", verifier_tools=brief.verifier_tools,
                             search_enabled=cfg.search_enabled)
    groups: list[tuple[dict, list[Agent]]] = []
    pool: list[Agent] = []
    for item in candidates:
        group = []
        for vote in range(cfg.verify_votes):
            a = Agent(id=f"verifier-{ids['n']}", role="verifier", model=cfg.agent_model(),
                      status="in_progress", input_items=build_verifier_input(root, brief, item),
                      meta={"item": item, "vote": vote})
            ids["n"] += 1
            agents.append(a)
            group.append(a)
            pool.append(a)
        groups.append((item, group))
    votes_note = f" × {cfg.verify_votes} votes" if cfg.verify_votes > 1 else ""
    on_event("verify", f"verifying {len(candidates)} item(s){votes_note}")

    def on_verdict(agent: Agent, fc: dict) -> str:
        try:
            agent.verdict = json.loads(fc["arguments"] or "{}")
        except json.JSONDecodeError:
            agent.verdict = None
        return json.dumps({"status": "recorded"})

    _run_pool(client, pool, vtools, "submit_verdict", on_verdict, cfg, tokens, root=root)
    steps["critical"] += max((a.rounds for a in pool), default=0)
    steps["total"] += sum(a.rounds for a in pool)

    return [(item, *_aggregate_votes(group, cfg.verify_votes == 1))
            for item, group in groups]


# --- synthesis -----------------------------------------------------------------


def _synthesize(client, brief, cfg, tokens, steps, n_files, confirmed, unverified,
                n_refuted, verified_stage, closing_note, errors) -> str:
    parts = [f"Files covered: {n_files}."]
    if verified_stage:
        parts.append(f"Confirmed results (JSON):\n{json.dumps(confirmed, indent=2)}")
        if unverified:
            parts.append("Unverified results — the verifier did not reach a verdict; "
                         "include them clearly flagged as unverified (JSON):\n"
                         f"{json.dumps(unverified, indent=2)}")
        if n_refuted:
            parts.append(f"Note: {n_refuted} candidate item(s) were refuted by "
                         "verification and excluded.")
    else:
        parts.append(f"Results (JSON):\n{json.dumps(confirmed + unverified, indent=2)}")
    if closing_note:
        parts.append(f"Orchestrator's closing notes:\n{closing_note}")
    parts.append("Write the report.")

    synth_input = [
        {"type": "message", "role": "system", "content": brief.synthesis_prompt},
        {"type": "message", "role": "user", "content": "\n\n".join(parts)},
    ]
    sresp = _dispatch(client, [_req(cfg.model, synth_input, max_output_tokens=16384,
                                    reasoning_effort=cfg.reasoning_effort)], cfg)[0]
    _add_tokens(tokens, sresp, cfg.model)
    steps["critical"] += 1
    steps["total"] += 1

    finish = R.finish_of(sresp)
    if finish == "error":
        err = sresp.get("_error", "request failed")
        errors.append(f"synthesis failed: {err}")
        return ("# Report generation failed\n\n"
                f"The synthesis call failed: {err}.\n\n"
                f"The structured results were preserved — see `{brief.result_key}.json`.")
    report = R.text_of(sresp) or "# Report\n\n(No report generated.)"
    if finish == "length":
        errors.append("synthesis output truncated at max_output_tokens")
        report += "\n\n> **Note:** this report was truncated at the output-token limit."
    return report


# --- top-level orchestration -----------------------------------------------


def run_swarm(client, brief: Brief, root, files, cfg: SwarmConfig, *,
              on_event=lambda *_: None) -> dict:
    tokens = _new_tokens()
    steps = {"critical": 0, "total": 0}
    ids = {"n": 0}
    agents: list[Agent] = []
    errors: list[str] = []
    results_tool = tools.submit_results_tool(brief.result_key, brief.result_schema,
                                             brief.submit_description)

    repo_map = repo.build_repo_map(root, files, max_chars=cfg.map_max_chars)
    orch = _orchestrator_agent(brief, cfg, repo_map, len(files))
    agents.append(orch)

    orchestrate = _orchestrate_kimi if cfg.interface == "kimi" else _orchestrate_structured
    all_results, assigned, waves = orchestrate(
        client, root, brief, files, cfg, tokens, steps, ids, agents, on_event,
        results_tool, orch)

    if _count_workers(agents) == 0:
        raise SwarmError("orchestrator dispatched no workers — the run produced nothing; "
                         "aborting rather than writing an empty report")
    failed_workers = [a for a in agents if a.role == "worker" and a.status == "failed"]
    if failed_workers:
        errors.append(f"{len(failed_workers)} worker call(s) failed "
                      f"({', '.join(a.id for a in failed_workers[:5])})")

    candidates = dedupe(all_results, brief.dedupe_key, brief.rank)
    on_event("dedupe", f"{len(all_results)} raw → {len(candidates)} unique")

    confirmed: list[dict] = []
    unverified: list[dict] = []
    n_refuted = 0
    verified_stage = bool(cfg.verify and brief.verifier_prompt and candidates)
    if verified_stage:
        for item, verification, verdict in _run_verifiers(
                client, root, brief, candidates, cfg, tokens, steps, ids, agents, on_event):
            it = dict(item)
            it["verdict"] = verdict
            it["verification"] = verification
            if verification == "confirmed":
                adj = _adjusted_severity(verdict)
                if adj:
                    it["severity"] = adj
                confirmed.append(it)
            elif verification == "unverified":
                unverified.append(it)
            else:
                n_refuted += 1
        if n_refuted:
            on_event("refuted", f"{n_refuted} item(s) refuted by verification")
        if unverified:
            on_event("unverified", f"{len(unverified)} item(s) kept unverified "
                                   "(verifier reached no verdict)")
    else:
        confirmed = [{**c, "verdict": None, "verification": "skipped"} for c in candidates]

    if brief.rank:
        confirmed.sort(key=brief.rank, reverse=True)
        unverified.sort(key=brief.rank, reverse=True)
    results = confirmed + unverified

    on_event("synthesize", f"writing report from {len(results)} item(s)")
    report = _synthesize(client, brief, cfg, tokens, steps, len(files), confirmed,
                         unverified, n_refuted, verified_stage,
                         orch.meta.get("closing_note"), errors)

    return {
        "report": report,
        "result_key": brief.result_key,
        "results": results,
        "n_refuted": n_refuted,
        "errors": errors,
        "interface": cfg.interface,
        "agents": [{"id": a.id, "role": a.role, "status": a.status, "rounds": a.rounds,
                    "meta": (a.meta if a.role != "verifier"
                             else {"item": a.meta.get("item", {}).get("title", "")}),
                    "n_results": len(a.results)} for a in agents],
        "tokens": tokens,
        "steps": {"critical": steps["critical"], "total": steps["total"],
                  "speedup": round(steps["total"] / max(steps["critical"], 1), 2)},
        "coverage": {"assigned": len(assigned & set(files)), "total": len(files)},
        "waves": waves,
    }
