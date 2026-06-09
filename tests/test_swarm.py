from src import swarm


def test_dedupe_merges_same_file_line_title():
    f = [{"file": "a.py", "line": 10, "title": "SQL injection", "severity": "high", "confidence": 0.8},
         {"file": "a.py", "line": 11, "title": "sql injection", "severity": "critical", "confidence": 0.6}]
    out = swarm.dedupe(f)
    assert len(out) == 1
    assert out[0]["severity"] == "critical"   # keeps highest severity
    assert out[0]["confidence"] == 0.8         # keeps max confidence


def test_run_audit_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f(x):\n    return run_command(x)\n")
    seq = iter([
        # 1) orchestrator: dispatch_workers
        [{"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5},
          "output": [{"type": "function_call", "call_id": "c1", "name": "dispatch_workers",
                      "arguments": '{"workers":[{"role":"injection","focus":"cmd inj","files":["a.py"]}]}'}]}],
        # 2) worker: report_findings
        [{"status": "completed", "usage": {"input_tokens": 8, "output_tokens": 4},
          "output": [{"type": "function_call", "call_id": "c2", "name": "report_findings",
                      "arguments": '{"findings":[{"severity":"high","title":"cmd inj","file":"a.py","line":2,"description":"unsanitized input to run_command","suggested_fix":"validate input","confidence":0.9}]}'}]}],
        # 3) orchestrator wave-2: stop (no tool call)
        [{"status": "completed", "usage": {"input_tokens": 6, "output_tokens": 2},
          "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}],
        # 4) verifier: confirm
        [{"status": "completed", "usage": {"input_tokens": 4, "output_tokens": 3},
          "output": [{"type": "function_call", "call_id": "c3", "name": "submit_verdict",
                      "arguments": '{"is_real":true,"confidence":0.85,"adjusted_severity":"high","reasoning":"reachable"}'}]}],
        # 5) synthesis: report text
        [{"status": "completed", "usage": {"input_tokens": 7, "output_tokens": 9},
          "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "# Audit\n1 finding"}]}]}],
    ])
    monkeypatch.setattr(swarm.R, "dispatch", lambda *a, **k: next(seq))
    cfg = swarm.SwarmConfig(model="m", max_waves=2)
    res = swarm.run_audit(client=None, root=str(tmp_path), files=["a.py"], cfg=cfg)
    assert res["report"].startswith("# Audit")
    assert len(res["findings"]) == 1 and res["findings"][0]["verdict"]["is_real"]
    assert res["tokens"]["input_tokens"] == 35   # 10+8+6+4+7
    assert res["coverage"] == {"assigned": 1, "total": 1}
    assert res["waves"] == 1


def test_run_audit_no_verify(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    seq = iter([
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "function_call", "call_id": "c1", "name": "dispatch_workers",
                      "arguments": '{"workers":[{"role":"r","focus":"f","files":["a.py"]}]}'}]}],
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "function_call", "call_id": "c2", "name": "report_findings",
                      "arguments": '{"findings":[{"severity":"low","title":"t","file":"a.py","description":"d","confidence":0.5}]}'}]}],
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}],
        # no verifier dispatch when verify=False; next is synthesis
        [{"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
          "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "# Report"}]}]}],
    ])
    monkeypatch.setattr(swarm.R, "dispatch", lambda *a, **k: next(seq))
    cfg = swarm.SwarmConfig(model="m", verify=False)
    res = swarm.run_audit(client=None, root=str(tmp_path), files=["a.py"], cfg=cfg)
    assert len(res["findings"]) == 1
    assert res["findings"][0]["verdict"] is None
