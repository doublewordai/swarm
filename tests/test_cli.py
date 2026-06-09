from click.testing import CliRunner

from src.cli import cli


def test_model_alias_and_dryrun(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    r = CliRunner().invoke(
        cli,
        ["audit", "--path", str(tmp_path), "-m", "k2.6", "--dry-run", "-o", str(tmp_path / "out")],
    )
    assert r.exit_code == 0, r.output
    assert "moonshotai/Kimi-K2.6" in r.output       # alias resolved
    assert "dispatch_workers" in r.output            # orchestrator tool shown


def test_audit_requires_one_source(tmp_path):
    r = CliRunner().invoke(cli, ["audit", "--dry-run", "-o", str(tmp_path / "out")])
    assert r.exit_code != 0
    assert "exactly one" in r.output
