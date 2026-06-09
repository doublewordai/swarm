import json
import os

from src import tools

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")


def test_tools_for_roles():
    assert {t["name"] for t in tools.tools_for("worker")} == {"read_file", "grep", "report_findings"}
    assert [t["name"] for t in tools.tools_for("orchestrator")] == ["dispatch_workers"]
    assert [t["name"] for t in tools.tools_for("verifier")] == ["submit_verdict"]


def test_tools_are_flat_open_responses_shape():
    # Spec-clean: function tools are flat (no chat-completions {"function": {...}} wrapper).
    for role in ("orchestrator", "worker", "verifier"):
        for t in tools.tools_for(role):
            assert t["type"] == "function"
            assert "name" in t and "parameters" in t
            assert "function" not in t


def test_execute_read_file():
    out = json.loads(tools.execute_tool("read_file", json.dumps({"path": "app.py"}), root=ROOT))
    assert "run_command(" in out["content"]


def test_execute_grep():
    out = json.loads(tools.execute_tool("grep", json.dumps({"pattern": r"run_command\("}), root=ROOT))
    assert any(h["file"] == "app.py" for h in out["hits"])


def test_deferred_returns_sentinel():
    assert tools.execute_tool("report_findings", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("dispatch_workers", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("submit_verdict", "{}", root=ROOT) == tools.DEFERRED
