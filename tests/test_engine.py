import json

import pytest

from src import engine
from src.briefs import get_brief


def _capture(monkeypatch, seq):
    """Monkeypatch engine.R.dispatch to serve `seq` and record every reqs batch."""
    it = iter(seq)
    captured = []

    def fake(client, reqs, **kw):
        captured.append(reqs)
        return next(it)

    monkeypatch.setattr(engine.R, "dispatch", fake)
    return captured


def _fc(name, args, call_id="c"):
    return {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
            "output": [{"type": "function_call", "call_id": call_id, "name": name,
                        "arguments": json.dumps(args)}]}


def _text(t="ok"):
    return {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": t}]}]}


def _failed(err="boom"):
    return {"status": "failed", "output": [], "usage": {}, "_error": err}


FINDING = {"severity": "high", "title": "cmd inj", "file": "a.py", "line": 2,
           "description": "d", "confidence": 0.9}


def test_dedupe_merges_and_ranks():
    items = [{"file": "a.py", "title": "X", "sev": 1}, {"file": "a.py", "title": "X", "sev": 3}]
    out = engine.dedupe(items, key_fn=lambda i: (i["file"], i["title"]), rank_fn=lambda i: i["sev"])
    assert len(out) == 1 and out[0]["sev"] == 3


def test_dedupe_no_key_keeps_all():
    assert len(engine.dedupe([{"a": 1}, {"a": 1}])) == 2


def _orch_dispatch(call_id="c1"):
    return [{"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
             "output": [{"type": "function_call", "call_id": call_id, "name": "dispatch_workers",
                         "arguments": '{"workers":[{"role":"r","focus":"f","files":["a.py"]}]}'}]}]


def _stop(tokens=2):
    return [{"status": "completed", "usage": {"input_tokens": 6, "output_tokens": tokens},
             "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}]


def _synth(text="# Report\n"):
    return [{"status": "completed", "usage": {"input_tokens": 7, "output_tokens": 9},
             "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}]


def test_run_swarm_audit_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f(x):\n    return run_command(x)\n")
    seq = iter([
        _orch_dispatch(),
        [{"status": "completed", "usage": {"input_tokens": 8, "output_tokens": 4},
          "output": [{"type": "function_call", "call_id": "c2", "name": "submit_results",
                      "arguments": '{"findings":[{"severity":"high","title":"cmd inj","file":"a.py","line":2,"description":"d","confidence":0.9}]}'}]}],
        _stop(),
        [{"status": "completed", "usage": {"input_tokens": 4, "output_tokens": 3},
          "output": [{"type": "function_call", "call_id": "c3", "name": "submit_verdict",
                      "arguments": '{"is_real":true,"confidence":0.85,"adjusted_severity":"high","reasoning":"reachable"}'}]}],
        _synth("# Audit\n1 finding"),
    ])
    monkeypatch.setattr(engine.R, "dispatch", lambda *a, **k: next(seq))
    cfg = engine.SwarmConfig(model="m", max_waves=2)
    res = engine.run_swarm(client=None, brief=get_brief("audit"), root=str(tmp_path), files=["a.py"], cfg=cfg)
    assert res["report"].startswith("# Audit")
    assert res["result_key"] == "findings"
    assert len(res["results"]) == 1 and res["results"][0]["verdict"]["is_real"]
    assert res["tokens"]["input_tokens"] == 35   # 10+8+6+4+7
    assert res["coverage"] == {"assigned": 1, "total": 1}


def test_run_swarm_onboarding_skips_verify(monkeypatch, tmp_path):
    # onboarding has no verifier_prompt → no verify dispatch even with cfg.verify=True
    (tmp_path / "a.py").write_text("x = 1\n")
    seq = iter([
        _orch_dispatch(),
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "function_call", "call_id": "c2", "name": "submit_results",
                      "arguments": '{"sections":[{"title":"t","purpose":"p","file":"a.py"}]}'}]}],
        _stop(1),
        _synth("# Guide"),
    ])
    monkeypatch.setattr(engine.R, "dispatch", lambda *a, **k: next(seq))
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"], engine.SwarmConfig(model="m"))
    assert res["result_key"] == "sections"
    assert len(res["results"]) == 1 and res["results"][0]["verdict"] is None


def test_worker_takes_a_tool_round_then_reports(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f(x):\n    return run_command(x)\n")
    seq = iter([
        _orch_dispatch(),
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "function_call", "call_id": "c2", "name": "grep",
                      "arguments": '{"pattern":"run_command"}'}]}],
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "function_call", "call_id": "c3", "name": "submit_results",
                      "arguments": '{"sections":[{"title":"t","purpose":"p","file":"a.py"}]}'}]}],
        _stop(1),
        _synth("# Guide"),
    ])
    monkeypatch.setattr(engine.R, "dispatch", lambda *a, **k: next(seq))
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"], engine.SwarmConfig(model="m"))
    assert len(res["results"]) == 1
    worker = next(a for a in res["agents"] if a["role"] == "worker")
    assert worker["rounds"] == 2  # grep round, then submit round


# --- failure paths -----------------------------------------------------------


def test_orchestrator_api_failure_raises_swarm_error(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    _capture(monkeypatch, [[_failed("ctx overflow")]])
    with pytest.raises(engine.SwarmError, match="orchestrator"):
        engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                         engine.SwarmConfig(model="m"))


def test_orchestrator_dispatching_no_workers_raises(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    _capture(monkeypatch, [[_text("nothing to do")]])
    with pytest.raises(engine.SwarmError, match="no workers"):
        engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                         engine.SwarmConfig(model="m"))


def test_synthesis_failure_yields_degraded_report_and_error(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "yes"})],
        [_failed("synth died")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m"))
    assert res["report"].startswith("# Report generation failed")
    assert any("synth" in e for e in res["errors"])
    assert len(res["results"]) == 1  # structured results survive


# --- submission validation ---------------------------------------------------


def test_invalid_submissions_dropped_not_crash(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    events = []
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": ["just a string", {"title": "missing fields"}, FINDING]})],
        [_text("done")],
        [_text("# Report")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", verify=False),
                           on_event=lambda k, m: events.append((k, m)))
    assert len(res["results"]) == 1
    assert any(k == "invalid" and "2" in m for k, m in events)


# --- worker loop -------------------------------------------------------------


def test_worker_forced_submit_after_round_exhaustion(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    captured = _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("grep", {"pattern": "run_command"})],          # round 1
        [_fc("grep", {"pattern": "run_command"})],          # round 2 (budget exhausted)
        [_fc("submit_results", {"sections": [{"title": "t", "purpose": "p", "file": "a.py"}]})],  # forced
        [_text("done")],
        [_text("# Guide")],
    ])
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", max_rounds=2))
    assert len(res["results"]) == 1
    worker = next(a for a in res["agents"] if a["role"] == "worker")
    assert worker["rounds"] == 3                            # 2 rounds + forced submit
    forced_req = captured[3][0]
    assert forced_req.get("tool_choice", {}).get("name") == "submit_results"


def test_worker_never_submitting_is_no_submit_and_reported_to_orchestrator(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_text("rambling, no tool call")],                  # round 1
        [_text("still no submission")],                     # forced turn ignored
        [_text("done")],
        [_text("# Guide")],
    ])
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", max_rounds=1))
    worker = next(a for a in res["agents"] if a["role"] == "worker")
    assert worker["status"] == "no_submit"
    # The orchestrator's next turn sees per-worker status and unreported files.
    feedback_items = [i for i in captured[3][0]["input_items"]
                      if i.get("type") == "function_call_output"]
    feedback = json.loads(feedback_items[-1]["output"])
    assert feedback["workers"][0]["status"] == "no_submit"
    assert "a.py" in feedback["files_unreported"]


def test_oversized_worker_spec_is_split(monkeypatch, tmp_path):
    files = [f"m{i}.py" for i in range(5)]
    for f in files:
        (tmp_path / f).write_text("x = 1\n")
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "big", "focus": "f", "files": files}]})],
        [_fc("submit_results", {"sections": []}, call_id=f"c{i}") for i in range(3)],
        [_text("done")],
        [_text("# Guide")],
    ])
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), files,
                           engine.SwarmConfig(model="m", max_files_per_worker=2))
    workers = [a for a in res["agents"] if a["role"] == "worker"]
    assert len(workers) == 3
    assert any("part 1/3" in w["meta"]["role"] for w in workers)


def test_worker_preload_respects_context_budget(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("A" * 200 + "\n")
    (tmp_path / "b.py").write_text("B" * 200 + "\n")
    captured = _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py", "b.py"]}]})],
        [_fc("submit_results", {"sections": []})],
        [_text("done")],
        [_text("# Guide")],
    ])
    engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py", "b.py"],
                     engine.SwarmConfig(model="m", worker_context_chars=250))
    worker_user_msg = captured[1][0]["input_items"][1]["content"]
    assert "### a.py" in worker_user_msg            # first file preloaded
    assert "### b.py" not in worker_user_msg        # second deferred...
    assert "b.py" in worker_user_msg                # ...but listed as fetchable
    assert "read_file" in worker_user_msg


# --- verifier stage ----------------------------------------------------------


def test_verifier_can_use_tools_before_verdict(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_fc("grep", {"pattern": "run_command"})],          # verifier investigates
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "reachable"})],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m"))
    assert len(res["results"]) == 1
    assert res["results"][0]["verification"] == "confirmed"
    verifier = next(a for a in res["agents"] if a["role"] == "verifier")
    assert verifier["rounds"] == 2


def test_verifier_without_verdict_yields_unverified_not_dropped(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_text("hmm, unsure")],                             # round 1: no verdict
        [_text("still no verdict")],                        # forced turn ignored
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", max_rounds=1))
    assert len(res["results"]) == 1
    assert res["results"][0]["verification"] == "unverified"
    assert res["results"][0]["verdict"] is None


def test_verify_vote_majority_confirms(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    votes = [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"}, "v1"),
             _fc("submit_verdict", {"is_real": True, "confidence": 0.8, "reasoning": "y"}, "v2"),
             _fc("submit_verdict", {"is_real": False, "confidence": 0.6, "reasoning": "n"}, "v3")]
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        votes,
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", verify_votes=3, max_rounds=1))
    assert len(res["results"]) == 1
    assert res["results"][0]["verification"] == "confirmed"
    assert len(res["results"][0]["verdict"]["votes"]) == 3


def test_verify_vote_majority_refutes(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    votes = [_fc("submit_verdict", {"is_real": False, "confidence": 0.9, "reasoning": "n"}, "v1"),
             _fc("submit_verdict", {"is_real": False, "confidence": 0.8, "reasoning": "n"}, "v2"),
             _fc("submit_verdict", {"is_real": True, "confidence": 0.6, "reasoning": "y"}, "v3")]
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        votes,
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", verify_votes=3, max_rounds=1))
    assert res["results"] == []
    assert res["n_refuted"] == 1


# --- metrics: critical steps + per-model tokens ------------------------------


def test_critical_steps_and_speedup(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [
            {"role": "wa", "focus": "f", "files": ["a.py"]},
            {"role": "wb", "focus": "f", "files": ["b.py"]}]})],
        # round 1: A submits, B greps
        [_fc("submit_results", {"findings": [FINDING]}, "ca"),
         _fc("grep", {"pattern": "y"}, "cb")],
        # round 2: B submits (empty)
        [_fc("submit_results", {"findings": []}, "cb2")],
        [_text("done")],
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"})],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py", "b.py"],
                           engine.SwarmConfig(model="m"))
    # critical: orch(1) + wave max(1,2)=2 + orch stop(1) + verify max(1) + synth(1) = 6
    # total:    orch(2) + workers(1+2) + verify(1) + synth(1) = 7
    assert res["steps"] == {"critical": 6, "total": 7, "speedup": 1.17}


def test_worker_model_routes_and_tokens_tracked_per_model(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"})],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="big", worker_model="small"))
    assert captured[0][0]["model"] == "big"      # orchestrator
    assert captured[1][0]["model"] == "small"    # worker
    assert captured[3][0]["model"] == "small"    # verifier
    assert captured[4][0]["model"] == "big"      # synthesis
    assert set(res["tokens"]["by_model"]) == {"big", "small"}
    assert res["tokens"]["input_tokens"] == sum(
        m["input_tokens"] for m in res["tokens"]["by_model"].values())


# --- orchestrator capability tools -------------------------------------------


def test_orchestrator_can_grep_before_dispatching(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    _capture(monkeypatch, [
        [_fc("grep", {"pattern": "run_command"})],
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"sections": [{"title": "t", "purpose": "p"}]})],
        [_text("done")],
        [_text("# Guide")],
    ])
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m"))
    assert len(res["results"]) == 1
    orch = next(a for a in res["agents"] if a["role"] == "orchestrator")
    assert orch["rounds"] == 3


# --- kimi interface mode ------------------------------------------------------


def test_kimi_interface_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    captured = _capture(monkeypatch, [
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [
              {"type": "function_call", "call_id": "k1", "name": "create_subagent",
               "arguments": json.dumps({"name": "hunter", "system_prompt": "You hunt bugs."})},
              {"type": "function_call", "call_id": "k2", "name": "assign_task",
               "arguments": json.dumps({"agent": "hunter", "prompt": "audit a.py",
                                        "files": ["a.py"]})},
          ]}],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("all done")],
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"})],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", interface="kimi"))
    assert len(res["results"]) == 1 and res["results"][0]["verification"] == "confirmed"
    orch_tools = {t["name"] for t in captured[0][0]["tools"]}
    assert {"create_subagent", "assign_task"} <= orch_tools
    worker_system = captured[1][0]["input_items"][0]["content"]
    assert "You hunt bugs." in worker_system        # orchestrator-authored persona
    assert "submit_results" in worker_system        # harness contract appended
    worker = next(a for a in res["agents"] if a["role"] == "worker")
    assert worker["meta"]["role"] == "hunter"


def test_kimi_unknown_agent_gets_error_and_zero_workers_raises(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, [
        [_fc("assign_task", {"agent": "ghost", "prompt": "do things"})],
        [_text("giving up")],
    ])
    with pytest.raises(engine.SwarmError, match="no workers"):
        engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                         engine.SwarmConfig(model="m", interface="kimi"))
    outputs = [i for i in captured[1][0]["input_items"]
               if i.get("type") == "function_call_output"]
    assert any("unknown agent" in o["output"] for o in outputs)


# --- large-repo scalability: compact input, directory-level decomposition -----


def test_dispatch_workers_paths_expand_to_files(monkeypatch, tmp_path):
    (tmp_path / "auth").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "auth" / "login.py").write_text("x = 1\n")
    (tmp_path / "auth" / "session.py").write_text("x = 1\n")
    (tmp_path / "db" / "models.py").write_text("x = 1\n")
    files = ["auth/login.py", "auth/session.py", "db/models.py"]
    captured = _capture(monkeypatch, [
        # orchestrator assigns a DIRECTORY, not every file
        [_fc("dispatch_workers", {"workers": [{"role": "auth", "focus": "f", "paths": ["auth"]}]})],
        [_fc("submit_results", {"sections": []})],
        [_text("done")],
        [_text("# Guide")],
    ])
    res = engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), files,
                           engine.SwarmConfig(model="m"))
    worker = next(a for a in res["agents"] if a["role"] == "worker")
    assert set(worker["meta"]["files"]) == {"auth/login.py", "auth/session.py"}
    assert res["coverage"]["assigned"] == 2


def test_orchestrator_gets_larger_output_budget_than_workers(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"sections": []})],
        [_text("done")],
        [_text("# Guide")],
    ])
    engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                     engine.SwarmConfig(model="m"))
    orch_budget = captured[0][0]["max_output_tokens"]
    worker_budget = captured[1][0]["max_output_tokens"]
    assert orch_budget > worker_budget
    assert orch_budget >= 16384


def test_default_map_budget_bounds_orchestrator_input(tmp_path):
    # The orchestrator's repo map is bounded by default (it must not balloon to a
    # 48k-token header-for-every-file dump that times out the call), yet still
    # names every file so nothing is invisible to decomposition.
    from src.tools import repo
    budget = engine.SwarmConfig(model="m").map_max_chars
    assert budget <= 100_000
    body = "\n".join(f"line_{i} = {i}  # padding to make files large" for i in range(120))
    for i in range(300):
        (tmp_path / f"m{i:03}.py").write_text(body + "\n")
    files = repo.list_source_files(str(tmp_path))
    m = repo.build_repo_map(str(tmp_path), files, max_chars=budget)
    assert len(m) <= budget             # bounded input
    assert all(f in m for f in files)   # every file still listed
    assert "line_0 " not in m           # large repo degraded away from full headers


# --- structured events for verbose logging -----------------------------------


def _events_of(monkeypatch, tmp_path, seq, cfg=None):
    events = []
    _capture(monkeypatch, seq)
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           cfg or engine.SwarmConfig(model="m", verify=False),
                           on_event=lambda k, m, data=None: events.append((k, m, data)))
    return events, res


def test_emits_per_call_events_with_timing_and_role(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    events, _ = _events_of(monkeypatch, tmp_path, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_text("# Audit")],
    ])
    calls = [d for k, _, d in events if k == "call"]
    roles = {c["role"] for c in calls}
    assert {"orchestrator", "worker", "synth"} <= roles
    for c in calls:
        assert "elapsed_s" in c and "tokens" in c and "finish" in c and "agent" in c


def test_call_event_carries_tool_calls_for_vv(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")
    events, _ = _events_of(monkeypatch, tmp_path, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("grep", {"pattern": "run_command"})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_text("# Audit")],
    ], cfg=engine.SwarmConfig(model="m", verify=False, max_rounds=3))
    worker_calls = [d for k, _, d in events if k == "call" and d["role"] == "worker"]
    assert any("grep" in c.get("tool_calls", []) for c in worker_calls)


def test_emits_plan_event_with_team(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    events, _ = _events_of(monkeypatch, tmp_path, [
        [_fc("dispatch_workers", {"workers": [
            {"role": "auth", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": []})],
        [_text("done")],
        [_text("# Audit")],
    ])
    plans = [d for k, _, d in events if k == "plan"]
    assert plans and plans[0]["workers"][0]["role"] == "auth"
    assert plans[0]["workers"][0]["n_files"] == 1


def test_failed_call_emitted_as_event(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    events, _ = _events_of(monkeypatch, tmp_path, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_failed("worker boom")],
        [_text("done")],
        [_text("# Audit")],
    ])
    failed = [d for k, _, d in events if k == "call" and d["finish"] == "error"]
    assert failed and failed[0]["role"] == "worker"


def test_on_event_back_compat_two_arg_handler(monkeypatch, tmp_path):
    # Handlers that only accept (kind, msg) must still work.
    (tmp_path / "a.py").write_text("x = 1\n")
    seen = []
    _capture(monkeypatch, [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"findings": []})],
        [_text("done")],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["a.py"],
                           engine.SwarmConfig(model="m", verify=False),
                           on_event=lambda k, m: seen.append((k, m)))
    assert any(k == "wave" for k, _ in seen)


# --- kimi flow alignment: task dispatch + worker self-discovery ---------------


def test_kimi_assign_task_schema_is_agent_prompt_only():
    from src import tools as T
    props = T.ASSIGN_TASK["parameters"]["properties"]
    assert set(props) == {"agent", "prompt"}        # paper E.8: no files/paths
    assert T.ASSIGN_TASK["parameters"]["required"] == ["agent", "prompt"]


def test_kimi_worker_self_gathers_no_preloaded_files(monkeypatch, tmp_path):
    (tmp_path / "auth.py").write_text("SECRET = 'preloaded-marker'\n")
    captured = _capture(monkeypatch, [
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [
              {"type": "function_call", "call_id": "k1", "name": "create_subagent",
               "arguments": json.dumps({"name": "a", "system_prompt": "Audit it."})},
              {"type": "function_call", "call_id": "k2", "name": "assign_task",
               "arguments": json.dumps({"agent": "a", "prompt": "audit the auth code"})}]}],
        # worker discovers the file itself, then submits
        [_fc("read_file", {"path": "auth.py"})],
        [_fc("submit_results", {"findings": [FINDING]})],
        [_text("done")],
        [_fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"})],
        [_text("# Audit")],
    ])
    res = engine.run_swarm(None, get_brief("audit"), str(tmp_path), ["auth.py"],
                           engine.SwarmConfig(model="m", interface="kimi"))
    worker_system = captured[1][0]["input_items"][0]["content"]
    worker_user = captured[1][0]["input_items"][1]["content"]
    assert "preloaded-marker" not in (worker_system + worker_user)  # contents NOT pre-loaded
    assert "auth.py" in worker_system                    # listed as discoverable (corpus)
    assert "read_file" in worker_system                  # told to self-gather
    assert "audit the auth code" in worker_user          # free-text task
    # coverage reflects what the worker actually read, not a pre-assignment
    assert res["coverage"] == {"assigned": 1, "total": 1}


def test_kimi_worker_gets_discovery_tools_even_if_brief_omits_them(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    # a brief whose worker_tools lack read_file/grep
    from src.briefs import Brief, register
    register(Brief(
        name="_kimi_probe", description="t", orchestrator_prompt="o", worker_prompt="w",
        synthesis_prompt="s", result_schema={"type": "object", "properties": {"x": {"type": "string"}},
                                              "required": ["x"]},
        result_key="rows", worker_tools=("run_sast",)))
    captured = _capture(monkeypatch, [
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [
              {"type": "function_call", "call_id": "k1", "name": "create_subagent",
               "arguments": json.dumps({"name": "a", "system_prompt": "go"})},
              {"type": "function_call", "call_id": "k2", "name": "assign_task",
               "arguments": json.dumps({"agent": "a", "prompt": "do it"})}]}],
        [_fc("submit_results", {"rows": []})],
        [_text("done")],
        [_text("# R")],
    ])
    engine.run_swarm(None, get_brief("_kimi_probe"), str(tmp_path), ["a.py"],
                     engine.SwarmConfig(model="m", interface="kimi"))
    worker_tool_names = {t["name"] for t in captured[1][0]["tools"]}
    assert {"read_file", "grep"} <= worker_tool_names


# --- request parameter overrides (provider compatibility) ---------------------


def _minimal_seq():
    return [
        [_fc("dispatch_workers", {"workers": [{"role": "r", "focus": "f", "files": ["a.py"]}]})],
        [_fc("submit_results", {"sections": [{"title": "t", "purpose": "p"}]})],
        [_text("done")],
        [_text("# Guide")],
    ]


def test_temperature_override_applies_to_all_roles(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, _minimal_seq())
    engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                     engine.SwarmConfig(model="m", temperature=0.7))
    for batch in captured:
        for req in batch:
            assert req["temperature"] == 0.7


def test_temperature_omit_drops_param_everywhere(monkeypatch, tmp_path):
    # gpt-5-class models 400 on any temperature — "omit" must remove the key.
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, _minimal_seq())
    engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                     engine.SwarmConfig(model="m", temperature="omit"))
    for batch in captured:
        for req in batch:
            assert "temperature" not in req


def test_temperature_default_keeps_role_defaults(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    captured = _capture(monkeypatch, _minimal_seq())
    engine.run_swarm(None, get_brief("onboarding"), str(tmp_path), ["a.py"],
                     engine.SwarmConfig(model="m"))
    assert captured[0][0]["temperature"] == 0.3   # orchestrator
    assert captured[1][0]["temperature"] == 0     # worker
