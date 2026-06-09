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
