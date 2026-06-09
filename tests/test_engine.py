from src import engine
from src.briefs import get_brief


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
