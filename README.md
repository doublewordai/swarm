# Doubleword Agent Swarm: a self-designing swarm you point at a task

A worked example of how to **execute an agent swarm** on the
[Doubleword](https://doubleword.ai) inference server using the
[Open Responses API](https://openresponses.org).

An LLM **orchestrator designs its own team**: it decomposes the work into parallel
subtasks, spawns **bounded-context workers** (each sees only its slice), an optional
**adversarial verifier** stage challenges each result, and a **synthesizer** writes the
report. *What* the swarm does is a **brief** — a small bundle of prompts, a result
schema, and a tool selection. It ships with two briefs and you can write your own:

- **`audit`** — point it at a repo, get a triaged bug/security report.
- **`onboarding`** — point it at a repo, get an architecture/onboarding guide.

The engine is brief-agnostic; the model is a runtime parameter (default
`moonshotai/Kimi-K2.6`); every call runs realtime (`priority`) or async
(`flex`+`background`).

> **Doubleword Agent Swarm is our interpretation of Moonshot's Kimi agent swarm**,
> reimplemented from scratch on open weights via the Open Responses API. Full credit for
> the idea goes to the original — see the
> [Kimi agent-swarm blog](https://www.kimi.com/blog/agent-swarm), the
> [Kimi K2.6 tech blog](https://www.kimi.com/blog/kimi-k2-6), and the
> [PARL paper](https://arxiv.org/html/2602.02276v1). (Kimi K2.6 is the default model,
> but the swarm is model-agnostic.)

To run it: install the [dw CLI](https://github.com/doublewordai/dw) and `dw login`,
or sign up at [app.doubleword.ai](https://app.doubleword.ai).

## How the swarm works

```
swarm run <brief> --repo owner/name
        │
   Repo map (code): shallow-clone → filter source files → compact tree + headers
        │   "map-first": the orchestrator can decompose immediately, no wasted round
        ▼
   Orchestrator (LLM): decomposes the task + designs the team — it picks the strategy
        │   and width itself.   tool: dispatch_workers([{role, focus, files}])
        ├─ Worker 0  scope: …  ─┐ bounded local context: only its files pre-loaded,
        ├─ Worker 1  scope: …   │ own memory — returns ONLY results (the brief's schema)
        └─ Worker K  scope: …  ─┘ worker tools come from the brief
        ▼
   Dedupe → (optional) Verifier swarm: independent skeptics challenge each result
        ▼
   Synthesizer (1 call): reconcile results → report.md + <results>.json
```

The loop is identical for every brief. A **brief** plugs in the prompts (orchestrator /
worker / verifier / synthesis), the **result schema** the workers emit, which **tools**
each role gets, and the dedupe/verify hooks. `audit` emits findings and verifies them;
`onboarding` emits doc sections and skips verification.

### Reimplementing Kimi's agent swarm on open weights

Kimi K2.5/K2.6 are RL-trained ([PARL](https://arxiv.org/html/2602.02276v1)) to be *good
at orchestrating* a swarm, but the spawning, parallel execution, context isolation, and
aggregation are **runtime scaffolding** that lives in Moonshot's hosted product, not in
the open weights. On Doubleword we have the raw model behind the Open Responses API, so
this harness *is* that scaffolding. It reproduces four principles:

1. **Self-designing orchestrator** — the model chooses the team and the decomposition.
2. **Bounded local context + route-back** — workers are isolated and return only results.
3. **Structural anti-groupthink** — independent verifiers refute before reconciliation.
4. **Synthesis** — one pass reconciles.

## Briefs

```bash
swarm briefs                 # list available briefs
```

| Brief | Does | Result | Verifier |
|-------|------|--------|----------|
| `audit` | finds bugs/security issues in a repo | `findings.json` (severity, file:line, fix) | yes (adversarial) |
| `onboarding` | documents a repo's subsystems | `sections.json` (purpose, components, deps) | no |

### Write your own brief

A brief is ~50 lines: prompts, a result schema, a tool selection. Drop a module in
`src/briefs/`, build a `Brief`, `register(...)` it, and `swarm run <name>` works — no
engine changes:

```python
# src/briefs/onboarding.py (abridged)
from . import Brief, register

register(Brief(
    name="onboarding",
    description="Document a codebase's subsystems for newcomers.",
    orchestrator_prompt="You are the lead author … call dispatch_workers once …",
    worker_prompt="Document ONLY your assigned files: purpose, key components, deps …",
    synthesis_prompt="Assemble an onboarding guide: overview, per-subsystem sections …",
    result_schema={"type": "object", "properties": {
        "title": {"type": "string"}, "purpose": {"type": "string"},
        "key_components": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}}},
        "required": ["title", "purpose"]},
    result_key="sections",
    worker_tools=("read_file", "grep"),
    verifier_prompt=None,          # set a prompt to enable the adversarial verify stage
))
```

(Today both briefs work over a Git repo. A non-repo brief — e.g. web research — is the
next seam: a `Corpus` abstraction for "what the swarm works over." See the roadmap.)

## Tools

Engine tools (every brief): `dispatch_workers` (orchestrator), `submit_results`
(worker terminal — schema is the brief's), `submit_verdict` (verifier terminal).
Capability tools a brief grants its workers/verifiers:

| Tool | Description | Execution |
|------|-------------|-----------|
| `read_file` | Read a repo file to follow an import/definition | Immediate |
| `grep` | Regex-search the repo to trace a value to its sink | Immediate |
| `run_sast` | Run static analysers (bandit/semgrep/…) — read-only | Immediate |
| `check_advisory` | Look up a dependency's CVEs on OSV (keyless) | Immediate |
| `web_search` / `read_page` | Ground a finding against docs/advisories (opt-in) | Immediate |

Built spec-clean against the Open Responses API: flat function tools, caller-owned
`input` items, `background`+poll for async, `service_tier` for tiers, `reasoning.effort`
to keep reasoning models like K2.6 responsive. No provider-specific workarounds.

## Tooling roadmap: Moonshot's framework → our read-only v1

Moonshot's hosted Kimi swarm hands each sub-agent a *tailored* slice of a broad toolbox
([K2.5](https://www.kimi.com/blog/kimi-k2-5) / [K2.6](https://www.kimi.com/blog/kimi-k2-6)
blogs, [PARL](https://arxiv.org/html/2602.02276v1)): web search & browsing, a
`code-interpreter` + `bash`, a file-edit suite (`createfile`/`insert`/`view`/`strreplace`/
`submit`), artifact generation (docs/sites/slides), database ops, computer-use, and
persistent memory. We reproduce the *pattern* and ship the tools in stages.

- **v1 (this repo): read-only.** Every tool is non-mutating to the target — the swarm
  reads (`read_file`/`grep`, `web_search`/`read_page`, `check_advisory`) and analyses
  (`run_sast`), but never changes the target. Safe to point at any repo, reproducible.
- **v2: write functionality (auto-fix).** A "fixer" brief + write tools (`apply_patch` /
  the edit suite), a sandboxed test-runner to validate the patch, branch/PR isolation —
  *audit → propose patch → verify patch → open a PR.*
- **Roadmap (unscheduled): the generalised tools.** A `Corpus` abstraction for non-repo
  briefs (web/files), sandboxed `code-interpreter` for dynamic proof-of-concept,
  persistent memory for continuous audits, browser/computer-use, artifact generation.

## Running it

```bash
dw login
dw examples clone swarm
cd swarm
dw project setup            # uv sync --extra sast (installs bandit for run_sast)
```

```bash
dw project run audit -- --repo psf/requests --max-files 20      # the audit brief
dw project run onboarding -- --path ./my-service                # the onboarding brief
dw project run report                                           # print the latest run
dw project run audit -- --repo psf/requests --dry-run           # plan only, no API calls
```

Or directly: `swarm run audit --repo … `, `swarm run onboarding --path …`, `swarm briefs`.

### The model is a runtime parameter

`--model` defaults to `moonshotai/Kimi-K2.6` and accepts an alias (`k2.6`, `k2.5`) or any
full `model_name` Doubleword serves — Kimi K2.6 is just the default, not a hard dependency.

### Useful flags

`--service-tier priority|flex` · `--background/--no-background` · `--max-files` (skipped
files logged) · `--max-agents` · `--max-waves` · `--max-rounds` · `--no-verify` ·
`--enable-search` (else on iff `SERPER_API_KEY` set) · `--dry-run`.

## Service tiers & measuring cost

The *same* swarm runs realtime (`--service-tier priority`) or async
(`--service-tier flex --background`). `swarm compare <brief> --repo …` runs both and
writes a wall-clock / token / cost table. See [`analysis.md`](analysis.md) — note that
whether `flex` is cheaper than `priority` depends on the model's configured tier pricing,
and `dw usage` is the source of truth for actual spend.

## Architecture

```
src/
├── cli.py            # `swarm run <brief>` / report / compare / briefs + results writing
├── engine.py         # the generic swarm loop (brief-agnostic): orchestrate → workers → verify → synthesize
├── responses.py      # spec-clean Open Responses client + dispatch (concurrent | background)
├── cost.py           # per-(model, tier) rate table + cost computation
├── briefs/
│   ├── __init__.py   # Brief dataclass + registry (register / get_brief / list_briefs)
│   ├── audit.py      # the audit brief
│   └── onboarding.py # the onboarding brief
└── tools/
    ├── __init__.py   # flat tool schemas + per-brief tool selection + execute_tool
    ├── repo.py       # clone / list / filter / read / grep + repo map (the repo corpus)
    ├── sast.py       # run_sast — static analysers
    ├── advisory.py   # check_advisory — OSV
    └── search.py     # web_search / read_page
```

`engine.run_swarm(client, brief, root, files, cfg)` is the core; it never mentions
audits. Results land in `results/<brief>-<slug>/{report.md, <results>.json,
swarm-tree.json, summary.json}`. Run the tests with `uv run pytest` — the engine is
covered end-to-end with a mocked dispatch (no network) across both briefs.

## Limitations & notes

- **Reasoning latency:** K2.6 reasons; even at `reasoning.effort=minimal` each call is
  tens of seconds, so a swarm takes minutes. A request timeout fails a stalled call
  gracefully rather than hanging the run.
- **Cost figures are a guide:** computed from the API's reported token usage; treat
  `dw usage` as the source of truth for actual spend.
- **Read-only:** results include suggestions as text; nothing is applied or executed.
- Large repos are sampled to `--max-files` (skipped files logged); the verifier stage
  reduces false positives but does not eliminate them.
