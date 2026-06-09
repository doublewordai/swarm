"""Dependency advisory lookup via OSV.dev (keyless).

Grounds dependency findings: given an ecosystem + package + version, returns the
real advisories (CVE/GHSA ids, summary, severity) instead of the model guessing.
"""

import requests

OSV_URL = "https://api.osv.dev/v1/query"

# Common lowercase inputs → OSV's case-sensitive ecosystem names.
_ECOSYSTEM = {
    "pypi": "PyPI", "python": "PyPI", "npm": "npm", "node": "npm",
    "go": "Go", "golang": "Go", "cargo": "crates.io", "rust": "crates.io",
    "crates.io": "crates.io", "maven": "Maven", "java": "Maven",
    "rubygems": "RubyGems", "ruby": "RubyGems", "gem": "RubyGems",
    "nuget": "NuGet", "packagist": "Packagist", "composer": "Packagist",
    "pub": "Pub", "hex": "Hex",
}


def _normalize_ecosystem(eco: str) -> str:
    return _ECOSYSTEM.get((eco or "").strip().lower(), eco)


def _parse_osv(data: dict) -> list[dict]:
    out = []
    for v in (data.get("vulns") or []):
        sev = ""
        for s in (v.get("severity") or []):
            sev = s.get("score", "") or sev
        out.append({
            "id": v.get("id"),
            "aliases": v.get("aliases", []),
            "summary": v.get("summary") or (v.get("details", "") or "")[:200],
            "severity": sev,
        })
    return out


def check_advisory(ecosystem: str, package: str, version: str | None = None,
                   timeout: int = 15) -> dict:
    """Query OSV for known advisories. Returns {package, version, ecosystem,
    vulnerable, advisories} or {error, advisories:[]}."""
    eco = _normalize_ecosystem(ecosystem)
    body: dict = {"package": {"name": package, "ecosystem": eco}}
    if version:
        body["version"] = version
    try:
        resp = requests.post(OSV_URL, json=body, timeout=timeout)
        resp.raise_for_status()
        advisories = _parse_osv(resp.json())
        return {"package": package, "version": version, "ecosystem": eco,
                "vulnerable": bool(advisories), "advisories": advisories}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"OSV query failed: {exc}", "advisories": []}
