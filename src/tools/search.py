"""Web search (Serper) + page fetch (Jina Reader) for grounding findings.

Opt-in grounding tools. ``web_search`` needs ``SERPER_API_KEY``; ``read_page`` is
keyless (r.jina.ai). Both are offered to agents only when search is enabled — see
``tools.tools_for(role, search_enabled=...)``. Used to confirm a *specific* suspected
issue against docs/advisories — reachability in the code stays the gate.
"""

import os

import requests

SERPER_URL = "https://google.serper.dev/search"
JINA_PREFIX = "https://r.jina.ai/"


def search_enabled() -> bool:
    """True when web search is configured (SERPER_API_KEY present)."""
    return bool(os.environ.get("SERPER_API_KEY"))


def _parse_serper(data: dict, max_results: int) -> list[dict]:
    out = []
    for item in (data.get("organic") or [])[:max_results]:
        out.append({"title": item.get("title"), "url": item.get("link"),
                    "snippet": item.get("snippet")})
    return out


def search(query: str, max_results: int = 5, timeout: int = 15) -> dict:
    """Targeted web search via Serper. Returns {query, results} or {error, results:[]}."""
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return {"error": "web_search unavailable (SERPER_API_KEY not set)", "results": []}
    try:
        resp = requests.post(SERPER_URL, headers={"X-API-KEY": key},
                             json={"q": query, "num": max_results}, timeout=timeout)
        resp.raise_for_status()
        return {"query": query, "results": _parse_serper(resp.json(), max_results)}
    except Exception as exc:  # noqa: BLE001 - tool errors are returned, not raised
        return {"error": f"search failed: {exc}", "results": []}


def fetch_page(url: str, max_chars: int = 20000, timeout: int = 20) -> dict:
    """Fetch a page as markdown via Jina Reader (keyless). Returns {url, content|error}."""
    try:
        resp = requests.get(JINA_PREFIX + url, headers={"Accept": "text/plain"}, timeout=timeout)
        resp.raise_for_status()
        return {"url": url, "content": resp.text[:max_chars]}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "error": f"fetch failed: {exc}"}
