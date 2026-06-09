from src.tools import sast

BANDIT = ('{"results":[{"filename":"app.py","line_number":10,"issue_severity":"MEDIUM",'
          '"issue_text":"Possible hardcoded password: changeme","test_id":"B105"}]}')
RUFF = ('[{"filename":"app.py","location":{"row":3,"column":1},"code":"F401",'
        '"message":"unused import"}]')
SEMGREP = ('{"results":[{"check_id":"py.audit.risky-pattern","path":"app.py",'
           '"start":{"line":5},"extra":{"message":"Potentially unsafe pattern","severity":"WARNING"}}]}')


def test_parse_bandit():
    assert sast._parse_bandit(BANDIT) == [{
        "tool": "bandit", "file": "app.py", "line": 10, "rule": "B105",
        "severity": "medium", "message": "Possible hardcoded password: changeme",
    }]


def test_parse_ruff():
    h = sast._parse_ruff(RUFF)[0]
    assert h["tool"] == "ruff" and h["file"] == "app.py" and h["line"] == 3 and h["rule"] == "F401"


def test_parse_semgrep():
    h = sast._parse_semgrep(SEMGREP)[0]
    assert h["tool"] == "semgrep" and h["line"] == 5 and h["severity"] == "warning"


def test_parsers_handle_garbage():
    assert sast._parse_bandit("not json") == []
    assert sast._parse_ruff("") == []
    assert sast._parse_semgrep("{") == []


def test_run_sast_no_analyzers(monkeypatch, tmp_path):
    monkeypatch.setattr(sast, "available_analyzers", lambda: [])
    out = sast.run_sast(str(tmp_path))
    assert out["ran"] == [] and out["hits"] == [] and "no SAST" in out["note"]


def test_available_analyzers(monkeypatch):
    monkeypatch.setattr(sast.shutil, "which", lambda t: "/usr/bin/" + t if t == "bandit" else None)
    assert sast.available_analyzers() == ["bandit"]
