# Kimi Swarm Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Doubleword example (sibling to `async-agents`) that runs a Kimi agent swarm to audit a codebase for bugs/security issues over the Open Responses API, runnable in realtime (`priority`) and async (`flex`+`background`) tiers with an honest cost comparison.

**Architecture:** One-level swarm — an LLM orchestrator decomposes the audit and dispatches bounded-context worker agents (one per file/subsystem/concern), which return only findings; a flat adversarial verifier stage challenges each finding; a final tool-free synthesis turn writes the report. A spec-clean Open Responses client dispatches each round either blocking-concurrent (realtime) or background-poll (async).

**Tech Stack:** Python 3.13, `uv`, `openai` (Responses API), `click`. Target model is a **runtime parameter** (`--model`, default `moonshotai/Kimi-K2.6`, any alias/`model_name` accepted). Run via `dw project run`.

> **Authoring note:** never write the literal tokens that the security hook blocks (the dynamic-eval builtin, the `os.system` call, etc.) into any file. Describe vulnerability *categories* by name (command injection, unsafe deserialization, dynamic code execution, SQL injection, hardcoded secrets). Test fixtures use a benign distinctive marker (`run_command(...)`), not real exploit code.

---

## Resolved facts (from build recon)

- **Models on Doubleword:** `moonshotai/Kimi-K2.6` (default; `CHAT`, 256K ctx, `reasoning`+`vision`, agent-swarm tuned) and `moonshotai/Kimi-K2.5`.
- **Model is a runtime param.** Aliases: `k2.6 → moonshotai/Kimi-K2.6`, `k2.5 → moonshotai/Kimi-K2.5`. Any other alias/full `model_name` passes through unchanged. Default `moonshotai/Kimi-K2.6`.
- **Auth:** `dw` CLI is logged in; `DOUBLEWORD_API_KEY` is not exported in the shell. Code reads `DOUBLEWORD_API_KEY`; run through `dw project run <step>` (injects it) or export a key from `dw keys create` for direct `uv run`.
- **Rates:** not in `dw models`; back out effective $/MTok from `dw usage` after a run and seed `cost.py`. Never invent.
- **`/v1/responses` conformance + tool-call format:** confirmed during the live smoke (Task 9). Code to spec; file a bug if it deviates.

## File structure

```
kimi-swarm-audit/
├── README.md                # narrative + cost tables (written Task 8)
├── analysis.md              # realtime vs async comparison (filled Task 9)
├── dw.toml                  # steps: audit, report, compare (Task 0)
├── pyproject.toml           # deps: click, openai (Task 0)
├── .gitignore               # results/, .venv/, __pycache__ (Task 0)
├── src/
│   ├── __init__.py
│   ├── responses.py         # Open Responses client + dispatch + parse helpers (Task 1)
│   ├── cost.py              # rate table keyed by model + compute_cost (Task 4)
│   ├── prompts.py           # ORCHESTRATOR/WORKER/VERIFIER/SYNTHESIS prompts (Task 5)
│   ├── swarm.py             # roles, state, orchestration loop, accounting (Task 6)
│   ├── cli.py               # audit/report/compare + results writing (Task 7)
│   └── tools/
│       ├── __init__.py      # flat tool schemas + execute_tool dispatch (Task 3)
│       └── repo.py          # clone/list/filter/read/grep + repo map (Task 2)
└── tests/
    ├── fixtures/            # tiny fake repo + recorded Response dicts
    ├── test_responses.py
    ├── test_repo.py
    ├── test_tools.py
    ├── test_cost.py
    └── test_swarm.py
```

Each module has one responsibility; `responses.py`/`repo.py`/`cost.py` are pure
and unit-tested with no network. `swarm.py` is tested with a fake `dispatch`.

---

## Task 0: Scaffold the project

**Files:** Create `pyproject.toml`, `dw.toml`, `.gitignore`, `src/__init__.py`, `src/tools/__init__.py` (placeholder), `tests/__init__.py`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "kimi-swarm-audit"
version = "0.1.0"
description = "Kimi agent swarm code audit via Doubleword Open Responses API"
requires-python = ">=3.11"
dependencies = [
    "click>=8.1.0",
    "openai>=1.99.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[project.scripts]
kimi-swarm-audit = "src.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Create `dw.toml`**

```toml
[project]
name = "kimi-swarm-audit"
setup = "uv sync"
workflow = [
    "dw project setup",
    "dw project run audit -- --repo psf/requests --max-files 20",
    "dw project run report",
]

[steps.audit]
description = "Run the Kimi agent swarm to audit a codebase"
run = "uv run kimi-swarm-audit audit"

[steps.report]
description = "Print the latest audit report"
run = "uv run kimi-swarm-audit report"

[steps.compare]
description = "Audit one repo in realtime and async tiers; write analysis.md"
run = "uv run kimi-swarm-audit compare"
```

- [ ] **Step 3: Create `.gitignore`**

```
.venv/
__pycache__/
*.pyc
results/
.pytest_cache/
```

- [ ] **Step 4: Create empty `src/__init__.py`, `src/tools/__init__.py`, `tests/__init__.py`** (one-line module docstrings).

- [ ] **Step 5: Sync deps** — `cd /Users/peter/titan/kimi-swarm-audit && uv sync --extra dev`. Expected: `.venv` created; `openai`, `click` resolve.

- [ ] **Step 6: Commit** (only if git initialized — repo is currently not git; skip otherwise).

---

## Task 1: `responses.py` — spec-clean Open Responses client

**Files:** Create `src/responses.py`, `tests/test_responses.py`.

Interface:

```python
TERMINAL = {"completed", "failed", "incomplete", "cancelled"}
def make_client(provider="doubleword") -> "OpenAI"
def text_of(resp: dict) -> str
def function_calls_of(resp: dict) -> list[dict]   # [{"call_id","name","arguments"}]
def usage_of(resp: dict) -> dict                  # {"input_tokens","output_tokens"}
def finish_of(resp: dict) -> str                  # "stop"|"tool_calls"|"length"|"error"
def call(client, *, model, input_items, tools=None, tool_choice=None,
         service_tier="priority", background=False,
         max_output_tokens=8192, temperature=0) -> dict
def poll(client, response_id, interval=3.0, timeout=1800) -> dict
def dispatch(client, requests, *, service_tier, background, max_concurrent=12) -> list[dict]
```

- [ ] **Step 1: Write failing tests for parse helpers**

```python
# tests/test_responses.py
from src import responses as R

MSG_RESP = {
    "id": "resp_1", "status": "completed", "model": "moonshotai/Kimi-K2.6",
    "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "hello world"}]},
    ],
    "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
}
TOOL_RESP = {
    "id": "resp_2", "status": "completed", "model": "m",
    "output": [{"type": "function_call", "call_id": "call_a", "name": "read_file",
                "arguments": "{\"path\": \"a.py\"}"}],
    "usage": {"input_tokens": 5, "output_tokens": 7},
}
LEN_RESP = {"id": "r", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [], "usage": {"input_tokens": 1, "output_tokens": 2}}

def test_text_of():
    assert R.text_of(MSG_RESP) == "hello world"
def test_function_calls_of():
    assert R.function_calls_of(TOOL_RESP) == [
        {"call_id": "call_a", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"}]
def test_usage_of():
    assert R.usage_of(MSG_RESP) == {"input_tokens": 11, "output_tokens": 3}
def test_finish_of():
    assert R.finish_of(MSG_RESP) == "stop"
    assert R.finish_of(TOOL_RESP) == "tool_calls"
    assert R.finish_of(LEN_RESP) == "length"
```

- [ ] **Step 2: Run → FAIL** — `uv run pytest tests/test_responses.py -q`

- [ ] **Step 3: Implement `responses.py`** — spec-clean: flat tools passed straight through; caller owns input items; background via `background=True` + `poll`.

```python
"""Spec-clean Open Responses client + dispatch. No provider-specific workarounds."""
import os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from openai import (OpenAI, APIConnectionError, APITimeoutError,
                    APIStatusError, RateLimitError)

TERMINAL = {"completed", "failed", "incomplete", "cancelled"}
PROVIDERS = {
    "doubleword": ("https://api.doubleword.ai/v1", "DOUBLEWORD_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}
_MAX_RETRIES, _RETRY_DELAY = 5, 1.0

def make_client(provider="doubleword"):
    base_url, env = PROVIDERS[provider]
    key = os.environ.get(env)
    if not key:
        raise RuntimeError(f"{env} not set (run via `dw project run` or export a key)")
    return OpenAI(api_key=key, base_url=base_url)

def _retry(fn):
    @wraps(fn)
    def w(*a, **k):
        last = None
        for i in range(_MAX_RETRIES):
            try: return fn(*a, **k)
            except (ConnectionError, OSError, APITimeoutError, APIConnectionError,
                    RateLimitError, APIStatusError) as e:
                last = e; time.sleep(_RETRY_DELAY * (i + 1))
        raise last
    return w

def text_of(resp):
    parts = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    parts.append(c.get("text", ""))
    return "".join(parts)

def function_calls_of(resp):
    out = []
    for item in resp.get("output", []):
        if item.get("type") == "function_call":
            out.append({"call_id": item.get("call_id") or item.get("id", ""),
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "")})
    return out

def usage_of(resp):
    u = resp.get("usage") or {}
    return {"input_tokens": u.get("input_tokens", 0) or 0,
            "output_tokens": u.get("output_tokens", 0) or 0}

def finish_of(resp):
    if function_calls_of(resp): return "tool_calls"
    status = resp.get("status", "completed")
    if status == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason")
        return "length" if reason == "max_output_tokens" else "incomplete"
    if status in ("failed", "cancelled"): return "error"
    return "stop"

@_retry
def call(client, *, model, input_items, tools=None, tool_choice=None,
         service_tier="priority", background=False,
         max_output_tokens=8192, temperature=0):
    body = {"model": model, "input": input_items, "temperature": temperature,
            "max_output_tokens": max_output_tokens, "service_tier": service_tier}
    if tools: body["tools"] = tools           # flat function tools, per spec
    if tool_choice: body["tool_choice"] = tool_choice
    if background: body["background"] = True
    return client.responses.create(**body).model_dump()

@_retry
def poll(client, response_id, interval=3.0, timeout=1800):
    waited = 0.0
    while True:
        resp = client.responses.retrieve(response_id).model_dump()
        if resp.get("status") in TERMINAL: return resp
        time.sleep(interval); waited += interval
        if waited >= timeout:
            return {"id": response_id, "status": "failed", "output": [],
                    "usage": {}, "_error": "poll timeout"}

def dispatch(client, requests, *, service_tier, background, max_concurrent=12):
    """Blocking → concurrent create. Background → submit all, poll all.
    A failed request becomes a failed resp dict; never raises."""
    if not background:
        results = [None] * len(requests)
        with ThreadPoolExecutor(max_workers=max_concurrent) as ex:
            futs = {ex.submit(call, client, service_tier=service_tier,
                              background=False, **req): i
                    for i, req in enumerate(requests)}
            for f in as_completed(futs):
                i = futs[f]
                try: results[i] = f.result()
                except Exception as e:
                    results[i] = {"status": "failed", "output": [], "usage": {},
                                  "_error": str(e)}
        return results
    ids = []
    for req in requests:
        try: ids.append(call(client, service_tier=service_tier, background=True, **req).get("id"))
        except Exception: ids.append(None)
    out = []
    for rid in ids:
        out.append(poll(client, rid) if rid else
                   {"status": "failed", "output": [], "usage": {}, "_error": "submit failed"})
    return out
```

- [ ] **Step 4: Run → PASS** — `uv run pytest tests/test_responses.py -q`
- [ ] **Step 5: Commit** (if git).

---

## Task 2: `tools/repo.py` — clone/list/filter/read/grep + repo map

**Files:** Create `src/tools/repo.py`, `tests/test_repo.py`, fixture repo `tests/fixtures/fakerepo/`.

Interface:

```python
SOURCE_EXT = {".py",".js",".ts",".tsx",".jsx",".go",".rs",".java",".rb",".php",
              ".c",".h",".cpp",".hpp",".cs",".scala",".kt",".swift",".sh"}
SKIP_DIRS = {".git","node_modules",".venv","venv","dist","build","vendor",
             "third_party","__pycache__",".pytest_cache",".mypy_cache","target"}
SKIP_SUFFIX = {".lock","min.js","min.css",".map"}
def resolve_source(repo, path, workdir) -> tuple[str,str]   # (root_dir, slug); repo→shallow clone
def list_source_files(root) -> list[str]                    # repo-relative, filtered, sorted
def read_file(root, rel, max_chars=60000) -> str
def grep(root, pattern, path=None, max_hits=80) -> list[dict]   # [{"file","line","text"}]
def build_repo_map(root, files, header_lines=40) -> str         # tree + sizes + headers
```

- [ ] **Step 1: Create fixture repo.** `tests/fixtures/fakerepo/app.py` holds a benign marker standing in for a planted issue:

```python
# tests/fixtures/fakerepo/app.py
def handler(req):
    user_input = req.args["q"]
    # SECURITY-FIXTURE: unsanitized input reaches a dynamic call (command-injection stand-in)
    return run_command(user_input)
```

Also `util.py` (one trivial function), `README.md` (any text), `pkg.lock` (any text), `node_modules/x.js` (any text).

- [ ] **Step 2: Write failing tests**

```python
# tests/test_repo.py
import os
from src.tools import repo
ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")

def test_list_filters_vendored_and_locks():
    files = repo.list_source_files(ROOT)
    assert "app.py" in files and "util.py" in files
    assert "pkg.lock" not in files
    assert not any(f.startswith("node_modules") for f in files)
def test_read_file():
    assert "run_command(" in repo.read_file(ROOT, "app.py")
def test_grep_finds_marker():
    hits = repo.grep(ROOT, r"run_command\(")
    assert any(h["file"] == "app.py" for h in hits)
def test_repo_map_lists_files():
    m = repo.build_repo_map(ROOT, repo.list_source_files(ROOT))
    assert "app.py" in m and "util.py" in m
```

- [ ] **Step 3: Run → FAIL.** **Step 4: Implement `repo.py`** — `os.walk` with `SKIP_DIRS` pruning + ext/suffix filter; `git clone --depth 1 https://github.com/<owner>/<name>` for `--repo`; capped `read_file`; `re`-based `grep` over listed files; `build_repo_map` joins relative path + byte size + first `header_lines` lines. **Step 5: Run → PASS.** **Step 6: Commit** (if git).

---

## Task 3: `tools/__init__.py` — flat tool schemas + execute_tool

**Files:** Modify `src/tools/__init__.py`, create `tests/test_tools.py`.

```python
DEFERRED = "__DEFERRED__"   # dispatch_workers, report_findings, submit_verdict
DISPATCH_WORKERS = {"type":"function","name":"dispatch_workers",
  "description":"Create parallel worker agents; each gets ONLY its files; returns their findings.",
  "parameters":{"type":"object","properties":{"workers":{"type":"array","items":{
     "type":"object","properties":{"role":{"type":"string"},"focus":{"type":"string"},
        "files":{"type":"array","items":{"type":"string"}}},
     "required":["role","focus","files"]}}},"required":["workers"]}}
READ_FILE = {"type":"function","name":"read_file","description":"Read one repo file.",
  "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}
GREP = {"type":"function","name":"grep","description":"Regex search across the repo.",
  "parameters":{"type":"object","properties":{"pattern":{"type":"string"},
     "path":{"type":"string"}},"required":["pattern"]}}
REPORT_FINDINGS = {"type":"function","name":"report_findings",
  "description":"Submit findings and finish.","parameters":{"type":"object","properties":{
     "findings":{"type":"array","items":{"type":"object","properties":{
        "severity":{"type":"string","enum":["critical","high","medium","low","info"]},
        "title":{"type":"string"},"file":{"type":"string"},"line":{"type":"integer"},
        "description":{"type":"string"},"suggested_fix":{"type":"string"},
        "confidence":{"type":"number"}},
        "required":["severity","title","file","description","confidence"]}}},
     "required":["findings"]}}
SUBMIT_VERDICT = {"type":"function","name":"submit_verdict",
  "description":"Confirm or refute a finding.","parameters":{"type":"object","properties":{
     "is_real":{"type":"boolean"},"confidence":{"type":"number"},
     "adjusted_severity":{"type":"string","enum":["critical","high","medium","low","info"]},
     "reasoning":{"type":"string"}},"required":["is_real","confidence","reasoning"]}}
ORCHESTRATOR_TOOLS=[DISPATCH_WORKERS]; WORKER_TOOLS=[READ_FILE,GREP,REPORT_FINDINGS]; VERIFIER_TOOLS=[SUBMIT_VERDICT]
def tools_for(role) -> list[dict]
def execute_tool(name, arguments, *, root) -> str   # JSON str, or DEFERRED for deferred tools
```

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tools.py
import json, os
from src import tools
ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "fakerepo")
def test_tools_for_roles():
    assert {t["name"] for t in tools.tools_for("worker")} == {"read_file","grep","report_findings"}
    assert [t["name"] for t in tools.tools_for("orchestrator")] == ["dispatch_workers"]
def test_execute_read_file():
    out = json.loads(tools.execute_tool("read_file", json.dumps({"path":"app.py"}), root=ROOT))
    assert "run_command(" in out["content"]
def test_deferred_returns_sentinel():
    assert tools.execute_tool("report_findings", "{}", root=ROOT) == tools.DEFERRED
    assert tools.execute_tool("dispatch_workers", "{}", root=ROOT) == tools.DEFERRED
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement** schemas + `tools_for` + `execute_tool` (immediate `read_file`/`grep` via `repo`; deferred → `DEFERRED`). **Step 4: Run → PASS.** **Step 5: Commit** (if git).

---

## Task 4: `cost.py` — model-keyed rate table + compute

**Files:** Create `src/cost.py`, `tests/test_cost.py`.

```python
# $ per 1M tokens (input, output) per tier. Seed real values from `dw usage` (Task 9).
RATES = {
    "moonshotai/Kimi-K2.6": {"priority": (0.0, 0.0), "flex": (0.0, 0.0)},
    "moonshotai/Kimi-K2.5": {"priority": (0.0, 0.0), "flex": (0.0, 0.0)},
}
def compute_cost(model, tier, input_tokens, output_tokens) -> dict
    # {"cost_usd": float|None, "rate_known": bool}
```

- [ ] **Step 1: Failing test**

```python
# tests/test_cost.py
from src import cost
def test_known_rate(monkeypatch):
    monkeypatch.setitem(cost.RATES, "m", {"priority": (1.0, 2.0)})
    r = cost.compute_cost("m", "priority", 1_000_000, 500_000)
    assert r["rate_known"] and abs(r["cost_usd"] - 2.0) < 1e-9
def test_unknown_rate():
    r = cost.compute_cost("nope", "flex", 10, 10)
    assert r["rate_known"] is False and r["cost_usd"] is None
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement.** **Step 4: Run → PASS.** **Step 5: Commit** (if git).

---

## Task 5: `prompts.py` — system prompts

**Files:** Create `src/prompts.py` with `ORCHESTRATOR_SYSTEM`, `WORKER_SYSTEM`, `VERIFIER_SYSTEM`, `SYNTHESIS_SYSTEM` (plain strings). Encode Kimi-derived behavior; reference vulnerability *categories* by name (command injection, unsafe deserialization, dynamic code execution, SQL injection, path traversal, SSRF, hardcoded secrets, auth bypass, race conditions) — do not embed literal exploit tokens.

- **Orchestrator:** lead auditor; repo map is in context; call `dispatch_workers` ONCE with a team; choose by-file / by-subsystem / by-security-concern; assign EVERY listed file to some worker (or omit with a reason); prefer ≤ max-agents workers.
- **Worker:** audit ONLY assigned files (pre-loaded); use `read_file`/`grep` to follow a lead; report concrete, reachable issues via `report_findings` with file+line+fix+confidence; return findings only.
- **Verifier:** skeptic; try to REFUTE the finding (reachable? real? false positive?); call `submit_verdict`; default `is_real=false` when uncertain.
- **Synthesis:** given confirmed findings, write a triaged Markdown audit (severity summary table, then per-finding sections with file:line, impact, fix); output only the report.

- [ ] **Step 1: Create file.** **Step 2:** `uv run python -c "import src.prompts"` → no error. **Step 3: Commit** (if git).

---

## Task 6: `swarm.py` — orchestration engine

**Files:** Create `src/swarm.py`, `tests/test_swarm.py`.

```python
@dataclass
class Agent:
    id: str; role: str; model: str
    input_items: list[dict] = field(default_factory=list)
    status: str = "pending"          # pending|in_progress|waiting|completed|failed
    rounds: int = 0
    findings: list[dict] = field(default_factory=list)
    verdict: dict | None = None
    meta: dict = field(default_factory=dict)

@dataclass
class SwarmConfig:
    model: str; service_tier: str = "priority"; background: bool = False
    max_agents: int = 12; max_files: int = 40; max_waves: int = 2
    max_rounds: int = 3; verify: bool = True; orchestrator_temperature: float = 0.3

def build_worker_input(root, role, focus, files) -> list[dict]
def build_verifier_input(root, finding) -> list[dict]
def dedupe(findings) -> list[dict]
def run_audit(client, root, files, cfg, *, on_event=lambda *_: None) -> dict
    # {"report","findings","agents","tokens","coverage":{"assigned","total"},"waves"}
```

Loop: build orchestrator (map-first) → `R.dispatch` orchestrator turn → on `dispatch_workers`, create bounded workers (each `build_worker_input`), `R.dispatch` the wave; workers may take ≤`max_rounds` (`read_file`/`grep`) then `report_findings`; route findings ONLY back into the orchestrator's `function_call_output` → orchestrator may dispatch another wave (≤`max_waves`) or stop → `dedupe` → if `verify`, one verifier per finding via `R.dispatch`, keep `is_real`, apply `adjusted_severity` → final tool-free synthesis `R.call` → assemble. Sum tokens from every `usage_of`. Failed agents recorded and skipped, never block a wave.

- [ ] **Step 1: Failing test — `dedupe`**

```python
# tests/test_swarm.py
from src import swarm
def test_dedupe_merges_same_file_line_title():
    f = [{"file":"a.py","line":10,"title":"SQL injection","severity":"high","confidence":0.8},
         {"file":"a.py","line":11,"title":"sql injection","severity":"critical","confidence":0.6}]
    out = swarm.dedupe(f)
    assert len(out) == 1
    assert out[0]["severity"] == "critical"   # keeps highest severity
```

- [ ] **Step 2: Failing test — engine with fake dispatch** (no network). The fixture file content is benign; the scripted worker "finds" an issue regardless:

```python
def test_run_audit_end_to_end(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("def f(x):\n    return run_command(x)\n")
    seq = iter([
        [{"status":"completed","usage":{"input_tokens":10,"output_tokens":5},
          "output":[{"type":"function_call","call_id":"c1","name":"dispatch_workers",
            "arguments":'{"workers":[{"role":"injection","focus":"cmd inj","files":["a.py"]}]}'}]}],
        [{"status":"completed","usage":{"input_tokens":8,"output_tokens":4},
          "output":[{"type":"function_call","call_id":"c2","name":"report_findings",
            "arguments":'{"findings":[{"severity":"high","title":"cmd inj","file":"a.py","line":2,"description":"unsanitized input to run_command","suggested_fix":"validate input","confidence":0.9}]}'}]}],
        [{"status":"completed","usage":{"input_tokens":6,"output_tokens":2},
          "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}]}],
        [{"status":"completed","usage":{"input_tokens":4,"output_tokens":3},
          "output":[{"type":"function_call","call_id":"c3","name":"submit_verdict",
            "arguments":'{"is_real":true,"confidence":0.85,"adjusted_severity":"high","reasoning":"reachable"}'}]}],
        [{"status":"completed","usage":{"input_tokens":7,"output_tokens":9},
          "output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"# Audit\n1 finding"}]}]}],
    ])
    monkeypatch.setattr(swarm.R, "dispatch", lambda *a, **k: next(seq))
    cfg = swarm.SwarmConfig(model="m", max_waves=2)
    res = swarm.run_audit(client=None, root=str(tmp_path), files=["a.py"], cfg=cfg)
    assert res["report"].startswith("# Audit")
    assert len(res["findings"]) == 1 and res["findings"][0]["verdict"]["is_real"]
    assert res["tokens"]["input_tokens"] == 35   # 10+8+6+4+7
```

- [ ] **Step 3: Run → FAIL.** **Step 4: Implement `swarm.py`** (`import src.responses as R`, plus `tools`, `prompts`, `tools.repo`). **Step 5: Run → PASS.** **Step 6: Commit** (if git).

---

## Task 7: `cli.py` — audit / report / compare

**Files:** Create `src/cli.py`, `tests/test_cli.py`.

```python
MODEL_ALIASES = {"k2.6":"moonshotai/Kimi-K2.6","k2.5":"moonshotai/Kimi-K2.5"}
DEFAULT_MODEL = "moonshotai/Kimi-K2.6"
# audit:  --repo/--path (one required), -m/--model (alias|id, default k2.6),
#         --service-tier [priority|flex] (default priority),
#         --background/--no-background (default: on iff flex),
#         --max-files 40 --max-agents 12 --max-waves 2 --max-rounds 3 --no-verify -o results/ --dry-run
# report: -o results/
# compare:--repo/--path, -m/--model, --max-files 20
```

`audit` resolves the alias, `repo.resolve_source`, lists & caps files (logging any dropped), builds `SwarmConfig`, makes client, times `run_audit`, writes `results/<slug>/{report.md,findings.json,swarm-tree.json,summary.json}` (summary: model, tier, background, tokens, wall_clock_s, cost via `cost.compute_cost`, coverage). `--dry-run` prints repo map + orchestrator tools, no API calls. `compare` runs the audit path twice (priority blocking; flex background) → `analysis.md`.

- [ ] **Step 1: Failing test — alias + dry-run** (CliRunner, no network)

```python
# tests/test_cli.py
from click.testing import CliRunner
from src.cli import cli
def test_model_alias_and_dryrun(tmp_path):
    (tmp_path/"a.py").write_text("x = 1\n")
    r = CliRunner().invoke(cli, ["audit","--path",str(tmp_path),"-m","k2.6",
                                 "--dry-run","-o",str(tmp_path/"out")])
    assert r.exit_code == 0
    assert "moonshotai/Kimi-K2.6" in r.output
    assert "dispatch_workers" in r.output
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement `cli.py`.** **Step 4: Run → PASS.** **Step 5: Commit** (if git).

---

## Task 8: README.md + analysis.md scaffold

**Files:** Create `README.md` (async-agents style: title, why-it-matters, swarm diagram, tools table, running via `dw project run`, cost-comparison referencing `analysis.md`, architecture tree, limitations). Create `analysis.md` with the comparison-table header and a note that numbers are filled by `compare`.

- [ ] **Step 1: Write README.md.** **Step 2: Write analysis.md skeleton.** **Step 3: Commit** (if git).

---

## Task 9: Live bring-up, smoke, rate seeding

- [ ] **Step 1: Full unit suite** — `cd /Users/peter/titan/kimi-swarm-audit && uv run pytest -q` → all pass.
- [ ] **Step 2: Dry run** — `uv run kimi-swarm-audit audit --path src -m k2.6 --dry-run` → repo map + `dispatch_workers`. (No key needed.)
- [ ] **Step 3: Provision a key** — `dw keys create --name kimi-swarm-scratch` then export `DOUBLEWORD_API_KEY` (or run via `dw project run audit`).
- [ ] **Step 4: Live smoke (realtime)** — `uv run kimi-swarm-audit audit --path src --max-files 8 --max-agents 4 --service-tier priority`. Confirm `/v1/responses` accepts flat tools + returns `function_call` items (open items #2/#3). On error: capture it and file a Doubleword bug — no workaround.
- [ ] **Step 5: Async smoke** — same with `--service-tier flex --background`. Confirm submit-then-poll.
- [ ] **Step 6: Seed real rates** — `dw usage --since $(date +%Y-%m-%d) --output json`; back out $/MTok per tier for `moonshotai/Kimi-K2.6`; write into `cost.RATES`. Re-run `report` to confirm `cost_usd` populates.
- [ ] **Step 7: `compare`** — `uv run kimi-swarm-audit compare --repo psf/requests --max-files 20`; fill `analysis.md` with measured wall-clock/tokens/cost for both tiers.
- [ ] **Step 8: Final commit** (if git).

---

## Self-review — spec coverage

- Orchestrator self-designs decomposition → Task 5 prompt + Task 6 `dispatch_workers`. ✔
- Bounded local context + route-back-only-findings → `build_worker_input` + findings into orchestrator `function_call_output` (Task 6). ✔
- Anti-groupthink verifier swarm → Task 6 verify stage + Task 5 verifier prompt + `--no-verify`. ✔
- Synthesis = orchestrator tool-free turn → Task 6 final `R.call`. ✔
- Spec-clean Open Responses (flat tools, background+poll, priority/flex), no DW hacks → Task 1. ✔
- Model runtime param, default Kimi-K2.6, swappable → Task 7 + `cost.py` keyed by model. ✔
- Realtime vs async comparison, measured tokens × rate table → Task 7 `compare` + Task 4 + Task 9. ✔
- Guardrails: filtering, no-silent-caps logging, width cap, coverage in summary → Tasks 2/6/7. ✔
- Outputs `results/<slug>/{report,findings,swarm-tree,summary}` → Task 7. ✔
- Tests: pure units + mocked-dispatch engine + CLI dry-run → Tasks 1–7. ✔
- Open items resolved first in live bring-up → Task 9. ✔
