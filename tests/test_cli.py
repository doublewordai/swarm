from click.testing import CliRunner

from src.cli import cli


def test_run_dryrun_audit(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "-m", "k2.6",
                                 "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "moonshotai/Kimi-K2.6" in r.output      # alias resolved
    assert "dispatch_workers" in r.output           # orchestrator tool
    assert "submit_results" in r.output             # worker terminal


def test_run_dryrun_onboarding_has_no_verify(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "onboarding", "--path", str(tmp_path),
                                 "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "no verify stage" in r.output


def test_unknown_brief(tmp_path):
    r = CliRunner().invoke(cli, ["run", "nope", "--path", str(tmp_path),
                                 "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "unknown brief" in r.output


def test_run_requires_one_source(tmp_path):
    r = CliRunner().invoke(cli, ["run", "audit", "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "exactly one" in r.output


def test_briefs_command_lists():
    r = CliRunner().invoke(cli, ["briefs"])
    assert r.exit_code == 0
    assert "audit" in r.output and "onboarding" in r.output


def test_run_dryrun_kimi_interface(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--interface", "kimi", "--dry-run",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "create_subagent" in r.output and "assign_task" in r.output


def test_run_dryrun_accepts_new_flags(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, [
        "run", "audit", "--path", str(tmp_path), "--dry-run", "-o", str(tmp_path / "out"),
        "--worker-model", "k2.5", "--verify-votes", "3", "--max-steps", "6",
        "--max-concurrent", "4", "--max-files-per-worker", "10"])
    assert r.exit_code == 0, r.output


def test_swarm_error_exits_nonzero(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")

    def boom(*a, **k):
        from src.engine import SwarmError
        raise SwarmError("orchestrator dispatched no workers")

    monkeypatch.setattr("src.cli.engine.run_swarm", boom)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "no workers" in r.output


def _fake_res(errors):
    return {
        "report": "# R", "result_key": "findings", "results": [], "n_refuted": 0,
        "errors": errors, "interface": "structured",
        "agents": [], "tokens": {"input_tokens": 1, "output_tokens": 1,
                                 "reasoning_tokens": 0, "by_model": {"m": {
                                     "input_tokens": 1, "output_tokens": 1,
                                     "reasoning_tokens": 0}}},
        "steps": {"critical": 4, "total": 6, "speedup": 1.5},
        "coverage": {"assigned": 1, "total": 1}, "waves": 1,
    }


def test_synthesis_failure_exits_nonzero_but_writes_results(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")
    monkeypatch.setattr("src.cli.engine.run_swarm",
                        lambda *a, **k: _fake_res(["synthesis failed: died"]))
    out = tmp_path / "out"
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "-o", str(out)])
    assert r.exit_code != 0
    assert "synthesis failed" in r.output
    assert (out / f"audit-{tmp_path.name.lower()}" / "findings.json").exists()


def test_nonfatal_errors_warn_but_exit_zero(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")
    monkeypatch.setattr("src.cli.engine.run_swarm",
                        lambda *a, **k: _fake_res(["2 worker call(s) failed (worker-1, worker-2)"]))
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "WARNING" in r.output and "worker call(s) failed" in r.output


def _run_seq(tmp_path):
    """A minimal mocked dispatch sequence: dispatch -> worker grep+submit -> stop -> verify -> synth."""
    import json as _j
    (tmp_path / "a.py").write_text("def f():\n    return run_command()\n")

    def fc(name, args, cid="c"):
        return {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
                "_elapsed_s": 1.23,
                "output": [{"type": "function_call", "call_id": cid, "name": name,
                            "arguments": _j.dumps(args)}]}

    def txt(t):
        return {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1},
                "_elapsed_s": 0.5,
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": t}]}]}

    return iter([
        [fc("dispatch_workers", {"workers": [{"role": "auth", "focus": "f", "files": ["a.py"]}]})],
        [fc("grep", {"pattern": "run_command"})],
        [fc("submit_results", {"findings": [{"severity": "high", "title": "x", "file": "a.py",
                                             "line": 2, "description": "d", "confidence": 0.9}]})],
        [txt("done")],
        [fc("submit_verdict", {"is_real": True, "confidence": 0.9, "reasoning": "y"})],
        [txt("# Audit")],
    ])


def _invoke(tmp_path, monkeypatch, extra_args):
    from src import responses as R
    seq = _run_seq(tmp_path)
    monkeypatch.setattr(R, "dispatch", lambda *a, **k: next(seq))
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")
    return CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                    "-o", str(tmp_path / "out")] + extra_args)


def test_default_verbosity_hides_per_call_lines(tmp_path, monkeypatch):
    r = _invoke(tmp_path, monkeypatch, [])
    assert r.exit_code == 0, r.output
    assert "tok  " not in r.output            # no per-call timing line at v=0
    assert "[plan]" not in r.output           # no plan at v=0
    assert "[wave]" in r.output               # headline events still shown


def test_v_shows_timing_tokens_and_plan(tmp_path, monkeypatch):
    r = _invoke(tmp_path, monkeypatch, ["-v"])
    assert r.exit_code == 0, r.output
    assert "tok" in r.output                  # per-call token count
    assert "1.2" in r.output                  # elapsed seconds rendered
    assert "[plan]" in r.output and "auth" in r.output   # dispatch plan shows the team


def test_vv_shows_tool_calls(tmp_path, monkeypatch):
    r = _invoke(tmp_path, monkeypatch, ["-vv"])
    assert r.exit_code == 0, r.output
    assert "grep" in r.output                 # worker's tool call traced at -vv


def test_v_not_shown_tool_calls(tmp_path, monkeypatch):
    r = _invoke(tmp_path, monkeypatch, ["-v"])
    assert "grep" not in r.output             # tool tracing is -vv only
