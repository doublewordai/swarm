from click.testing import CliRunner

from src.cli import cli


def test_run_dryrun_audit_defaults_to_kimi(tmp_path):
    # No --interface → kimi (the default); the orchestrator gets the trained surface.
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "-m", "k2.6",
                                 "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "moonshotai/Kimi-K2.6" in r.output                       # alias resolved
    assert "create_subagent" in r.output and "assign_task" in r.output  # kimi default
    assert "submit_results" in r.output                            # worker terminal


def test_run_dryrun_structured_interface(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--interface", "structured", "--dry-run",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "dispatch_workers" in r.output           # structured orchestrator tool


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
    # _run_seq mocks the structured dispatch_workers flow, so pin the interface.
    return CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                    "--interface", "structured",
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


def test_provider_openai_flows_to_make_client(tmp_path, monkeypatch):
    from src import responses as R
    seen = {}

    def fake_make_client(provider="doubleword", timeout=600.0):
        seen["provider"] = provider
        return object()

    monkeypatch.setattr(R, "make_client", fake_make_client)
    monkeypatch.setattr("src.cli.engine.run_swarm", lambda *a, **k: _fake_res([]))
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--provider", "openai", "-m", "gpt-5.2",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["provider"] == "openai"


def test_provider_openai_requires_explicit_model(tmp_path):
    # The default model is a Doubleword alias; refusing to guess an OpenAI model
    # beats sending "k2.6" to api.openai.com.
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--provider", "openai", "--dry-run",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "-m" in r.output or "--model" in r.output


def test_model_alias_not_remapped_for_openai(tmp_path, monkeypatch):
    from src import responses as R
    seen = {}
    monkeypatch.setattr(R, "make_client", lambda provider="doubleword", timeout=600.0: object())

    def capture(client, b, root, files, cfg, **k):
        seen["model"] = cfg.model
        return _fake_res([])

    monkeypatch.setattr("src.cli.engine.run_swarm", capture)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--provider", "openai", "-m", "gpt-5.2",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["model"] == "gpt-5.2"


def test_reasoning_effort_and_temperature_flags_reach_config(tmp_path, monkeypatch):
    from src import responses as R
    seen = {}
    monkeypatch.setattr(R, "make_client", lambda provider="doubleword", timeout=600.0: object())
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")

    def capture(client, b, root, files, cfg, **k):
        seen["effort"] = cfg.reasoning_effort
        seen["temp"] = cfg.temperature
        return _fake_res([])

    monkeypatch.setattr("src.cli.engine.run_swarm", capture)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--reasoning-effort", "high", "--temperature", "0.7",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["effort"] == "high"
    assert seen["temp"] == 0.7


def test_none_values_omit_params(tmp_path, monkeypatch):
    # gpt-5-class reasoning models reject temperature; non-reasoning models reject
    # reasoning.effort — "none" must mean "omit the parameter", not "send null".
    from src import responses as R
    seen = {}
    monkeypatch.setattr(R, "make_client", lambda provider="doubleword", timeout=600.0: object())
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")

    def capture(client, b, root, files, cfg, **k):
        seen["effort"] = cfg.reasoning_effort
        seen["temp"] = cfg.temperature
        return _fake_res([])

    monkeypatch.setattr("src.cli.engine.run_swarm", capture)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--reasoning-effort", "none", "--temperature", "none",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["effort"] is None
    assert seen["temp"] == "omit"


def test_solo_dryrun_shows_solo_mode(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "--solo",
                                 "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "solo" in r.output.lower()
    assert "dispatch_workers" not in r.output     # no orchestration in solo


def test_solo_flows_to_cfg_with_big_context(tmp_path, monkeypatch):
    from src import responses as R
    seen = {}
    monkeypatch.setattr(R, "make_client", lambda provider="doubleword", timeout=600.0: object())

    def capture(client, b, root, files, cfg, **k):
        seen["solo"] = cfg.solo
        seen["ctx"] = cfg.worker_context_chars
        seen["out"] = cfg.worker_max_output_tokens
        return _fake_res([])

    monkeypatch.setattr("src.cli.engine.run_swarm", capture)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "--solo",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["solo"] is True
    assert seen["ctx"] >= 1_000_000               # bumped for a big single context
    assert seen["out"] > 8192                      # room to emit many findings at once


def _cfg_capture(monkeypatch):
    from src import responses as R
    seen = {}
    monkeypatch.setattr(R, "make_client", lambda provider="doubleword", timeout=600.0: object())

    def capture(client, b, root, files, cfg, **k):
        seen["ctx"] = cfg.worker_context_chars
        seen["out"] = cfg.worker_max_output_tokens
        return _fake_res([])

    monkeypatch.setattr("src.cli.engine.run_swarm", capture)
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "x")
    return seen


def test_context_chars_and_output_flags_override_solo_defaults(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    seen = _cfg_capture(monkeypatch)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "--solo",
                                 "--context-chars", "800000", "--max-output-tokens", "16384",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["ctx"] == 800_000 and seen["out"] == 16_384


def test_context_chars_flag_applies_without_solo(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    seen = _cfg_capture(monkeypatch)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path),
                                 "--context-chars", "500000", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["ctx"] == 500_000


def test_solo_budget_defaults_unchanged_without_flags(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    seen = _cfg_capture(monkeypatch)
    r = CliRunner().invoke(cli, ["run", "audit", "--path", str(tmp_path), "--solo",
                                 "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert seen["ctx"] == 3_000_000 and seen["out"] == 32_768
