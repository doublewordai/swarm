from src.tools import search

SERPER = {"organic": [
    {"title": "T", "link": "http://x", "snippet": "s"},
    {"title": "T2", "link": "http://y", "snippet": "s2"},
]}


def test_parse_serper():
    assert search._parse_serper(SERPER, 5) == [
        {"title": "T", "url": "http://x", "snippet": "s"},
        {"title": "T2", "url": "http://y", "snippet": "s2"},
    ]


def test_parse_serper_caps():
    assert len(search._parse_serper(SERPER, 1)) == 1


def test_search_no_key(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    out = search.search("q")
    assert out["results"] == [] and "SERPER_API_KEY" in out["error"]


def test_search_enabled(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert search.search_enabled() is False
    monkeypatch.setenv("SERPER_API_KEY", "x")
    assert search.search_enabled() is True
