import json
import os

from src import tools

ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")
RTOOL = tools.submit_results_tool("findings", {"type": "object"}, "submit")


def test_tools_for_roles():
    w = {t["name"] for t in tools.tools_for(
        "worker", worker_tools=("read_file", "grep", "run_sast", "check_advisory"), results_tool=RTOOL)}
    assert w == {"read_file", "grep", "run_sast", "check_advisory", "submit_results"}
    v = {t["name"] for t in tools.tools_for("verifier", verifier_tools=("check_advisory", "run_sast"))}
    assert v == {"check_advisory", "run_sast", "submit_verdict"}


def test_orchestrator_tools_structured_interface():
    # The orchestrator can probe the repo before/between waves, not only delegate.
    names = {t["name"] for t in tools.tools_for("orchestrator")}
    assert names == {"dispatch_workers", "read_file", "grep"}


def test_orchestrator_tools_kimi_interface():
    # Kimi's trained interface (K2.5 tech report, Appendix E.8).
    names = {t["name"] for t in tools.tools_for("orchestrator", interface="kimi")}
    assert names == {"create_subagent", "assign_task", "read_file", "grep"}
    by_name = {t["name"]: t for t in tools.tools_for("orchestrator", interface="kimi")}
    assert by_name["create_subagent"]["parameters"]["required"] == ["name", "system_prompt"]
    assert by_name["assign_task"]["parameters"]["required"] == ["agent", "prompt"]


def test_kimi_tools_are_deferred():
    assert tools.execute_tool("create_subagent", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("assign_task", "{}", root=ROOT) == tools.DEFERRED


def test_search_tools_are_opt_in():
    names = ("read_file", "web_search", "read_page")
    off = {t["name"] for t in tools.tools_for("worker", worker_tools=names, results_tool=RTOOL)}
    assert "web_search" not in off and "read_page" not in off
    on = {t["name"] for t in tools.tools_for("worker", worker_tools=names, search_enabled=True, results_tool=RTOOL)}
    assert {"web_search", "read_page"} <= on


def test_submit_results_tool_shape():
    t = tools.submit_results_tool("sections", {"type": "object", "properties": {"x": {"type": "string"}}}, "d")
    assert t["type"] == "function" and t["name"] == "submit_results"
    props = t["parameters"]["properties"]
    assert "sections" in props and props["sections"]["type"] == "array"


def test_tools_are_flat_open_responses_shape():
    schemas = (tools.tools_for("orchestrator")
               + tools.tools_for("worker", worker_tools=("read_file", "grep", "run_sast",
                                                         "check_advisory", "web_search", "read_page"),
                                 search_enabled=True, results_tool=RTOOL)
               + tools.tools_for("verifier", verifier_tools=("run_sast",)))
    for t in schemas:
        assert t["type"] == "function" and "name" in t and "parameters" in t
        assert "function" not in t


def test_execute_read_file():
    out = json.loads(tools.execute_tool("read_file", json.dumps({"path": "app.py"}), root=ROOT))
    assert "run_command(" in out["content"]


def test_execute_grep():
    out = json.loads(tools.execute_tool("grep", json.dumps({"pattern": r"run_command\("}), root=ROOT))
    assert any(h["file"] == "app.py" for h in out["hits"])


def test_execute_run_sast(monkeypatch):
    monkeypatch.setattr(tools.sast, "run_sast", lambda root, path=None: {"ran": ["bandit"], "hits": []})
    out = json.loads(tools.execute_tool("run_sast", "{}", root=ROOT))
    assert out["ran"] == ["bandit"]


def test_execute_check_advisory(monkeypatch):
    monkeypatch.setattr(tools.advisory, "check_advisory",
                        lambda eco, pkg, ver=None: {"vulnerable": True, "advisories": [{"id": "X"}]})
    out = json.loads(tools.execute_tool(
        "check_advisory", json.dumps({"ecosystem": "PyPI", "package": "p"}), root=ROOT))
    assert out["vulnerable"] is True


def test_deferred_returns_sentinel():
    assert tools.execute_tool("submit_results", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("dispatch_workers", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("submit_verdict", "{}", root=ROOT) == tools.DEFERRED
