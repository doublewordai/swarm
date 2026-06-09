import json
import os

from src import tools

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")


def test_tools_for_roles():
    assert {t["name"] for t in tools.tools_for("worker")} == {
        "read_file", "grep", "run_sast", "check_advisory", "report_findings"}
    assert {t["name"] for t in tools.tools_for("verifier")} == {
        "check_advisory", "run_sast", "submit_verdict"}
    assert [t["name"] for t in tools.tools_for("orchestrator")] == ["dispatch_workers"]


def test_search_tools_are_opt_in():
    assert "web_search" not in {t["name"] for t in tools.tools_for("worker")}
    w = {t["name"] for t in tools.tools_for("worker", search_enabled=True)}
    assert {"web_search", "read_page"} <= w
    v = {t["name"] for t in tools.tools_for("verifier", search_enabled=True)}
    assert {"web_search", "read_page"} <= v


def test_tools_are_flat_open_responses_shape():
    for role in ("orchestrator", "worker", "verifier"):
        for t in tools.tools_for(role, search_enabled=True):
            assert t["type"] == "function"
            assert "name" in t and "parameters" in t
            assert "function" not in t


def test_execute_read_file():
    out = json.loads(tools.execute_tool("read_file", json.dumps({"path": "app.py"}), root=ROOT))
    assert "run_command(" in out["content"]


def test_execute_grep():
    out = json.loads(tools.execute_tool("grep", json.dumps({"pattern": r"run_command\("}), root=ROOT))
    assert any(h["file"] == "app.py" for h in out["hits"])


def test_execute_run_sast(monkeypatch):
    monkeypatch.setattr(tools.sast, "run_sast",
                        lambda root, path=None: {"ran": ["bandit"], "hits": [{"tool": "bandit"}]})
    out = json.loads(tools.execute_tool("run_sast", "{}", root=ROOT))
    assert out["ran"] == ["bandit"] and out["hits"][0]["tool"] == "bandit"


def test_execute_check_advisory(monkeypatch):
    monkeypatch.setattr(tools.advisory, "check_advisory",
                        lambda eco, pkg, ver=None: {"vulnerable": True, "advisories": [{"id": "X"}]})
    out = json.loads(tools.execute_tool(
        "check_advisory", json.dumps({"ecosystem": "PyPI", "package": "p", "version": "1"}), root=ROOT))
    assert out["vulnerable"] is True


def test_execute_web_search(monkeypatch):
    monkeypatch.setattr(tools.search, "search",
                        lambda q, max_results=5: {"query": q, "results": [{"url": "u"}]})
    out = json.loads(tools.execute_tool("web_search", json.dumps({"query": "q"}), root=ROOT))
    assert out["results"][0]["url"] == "u"


def test_deferred_returns_sentinel():
    assert tools.execute_tool("report_findings", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("dispatch_workers", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("submit_verdict", "{}", root=ROOT) == tools.DEFERRED
