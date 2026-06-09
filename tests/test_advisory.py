from src.tools import advisory

OSV = {"vulns": [{
    "id": "GHSA-xxxx", "summary": "Bad bug", "aliases": ["CVE-2024-0001"],
    "severity": [{"type": "CVSS_V3", "score": "7.5"}],
}]}


def test_parse_osv():
    a = advisory._parse_osv(OSV)
    assert a[0]["id"] == "GHSA-xxxx"
    assert a[0]["summary"] == "Bad bug"
    assert "CVE-2024-0001" in a[0]["aliases"]
    assert a[0]["severity"] == "7.5"


def test_parse_osv_empty():
    assert advisory._parse_osv({}) == []


def test_normalize_ecosystem():
    assert advisory._normalize_ecosystem("pypi") == "PyPI"
    assert advisory._normalize_ecosystem("npm") == "npm"
    assert advisory._normalize_ecosystem("Unknown") == "Unknown"


def test_check_advisory_mock(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return OSV

    monkeypatch.setattr(advisory.requests, "post", lambda *a, **k: _Resp())
    out = advisory.check_advisory("pypi", "requests", "2.5.0")
    assert out["vulnerable"] is True
    assert out["ecosystem"] == "PyPI"
    assert out["advisories"][0]["id"] == "GHSA-xxxx"
