# Doubleword Agent Swarm: a self-designing swarm you point at a task

Run an **agent swarm** on the [Doubleword](https://doubleword.ai) inference server via the
[Open Responses API](https://openresponses.org). An LLM **orchestrator designs its own
team**: it decomposes the work, fans out **bounded-context workers** (each sees only its
slice), an optional **adversarial verifier** challenges each result, and a **synthesizer**
writes the report.

*What* the swarm does is a **brief** — a small bundle of prompts, a result schema, and a
tool selection. Two ship in the box (and you can write your own):

| Brief | Point it at a repo, get… | Verifier |
|-------|--------------------------|----------|
| `audit` | a triaged bug/security report — `findings.json` (severity, `file:line`, fix) + `report.md` | yes (adversarial) |
| `onboarding` | an architecture/onboarding guide — `sections.json` (purpose, components, deps) + `report.md` | no |

The engine is brief-agnostic and model-agnostic (default `moonshotai/Kimi-K2.6`).

> **This is our interpretation of Moonshot's Kimi agent swarm**, reimplemented from scratch
> on open weights. Full credit to the original — the
> [Kimi agent-swarm blog](https://www.kimi.com/blog/agent-swarm) and the
> [PARL paper / K2.5 report](https://arxiv.org/html/2602.02276v1). See
> [Faithful to Kimi](#faithful-to-kimi) for what we reproduced and what we dropped.

## Quickstart (dw CLI)

```bash
dw login
dw examples clone swarm
cd swarm
dw project setup            # uv sync --extra sast (installs bandit for run_sast)
```

Then run a brief:

```bash
dw project run audit      -- --repo psf/requests --max-files 20   # audit a GitHub repo
dw project run onboarding -- --path ./my-service                  # document a local dir
dw project run audit      -- --repo psf/requests --dry-run        # plan only, no API calls
dw project run report                                             # print the latest run
```

`--repo owner/name` shallow-clones from GitHub; `--path` works over a local directory (no
remote needed). Inside the cloned project you can also call the CLI directly: `swarm run
audit --repo …`, `swarm briefs`.

**What you get** — `results/<brief>-<slug>/`:

| File | Contents |
|------|----------|
| `report.md` | the synthesized, human-readable report |
| `findings.json` / `sections.json` | the structured results (machine-readable) |
| `swarm-tree.json` | the agents the orchestrator spawned — roles, scopes, status |
| `summary.json` | model, tokens, cost, coverage, and critical/total step counts |

## How it works

```
swarm run <brief> --repo owner/name
        │
   Repo map (code): shallow-clone → filter source files → compact tree + headers
        │   degrades (headers → tree → truncated) to fit the orchestrator's context budget
        ▼
   Orchestrator (LLM): decomposes the task + designs the team — picks strategy and width
        │   itself; authors specialist personas + dispatches scoped tasks (kimi, default)
        │   or assigns directories (structured); can read_file/grep to probe first.
        ├─ Worker 0  scope: …  ─┐ bounded local context: self-gathered (kimi) or pre-loaded
        ├─ Worker 1  scope: …   │ (structured), own memory — returns
        └─ Worker K  scope: …  ─┘ ONLY schema-valid results (its research is discarded).
        ▼   route-back: per-worker status + unreported files → orchestrator may fill gaps
   Dedupe → (optional) Verifier panel: N independent skeptics per item (majority vote).
        │   confirmed → kept · refuted → dropped (counted) · no verdict → kept, flagged.
        ▼
   Synthesizer (1 call): reconcile confirmed + unverified → report.md + <results>.json
```

The loop is identical for every brief. A **brief** plugs in the prompts (orchestrator /
worker / verifier / synthesis), the **result schema** workers emit (enforced — invalid
items are dropped, not trusted), which **tools** each role gets, and dedupe/verify hooks.

**Failure is loud.** A dead orchestrator call or a run that dispatches zero workers raises
`SwarmError` and exits non-zero — it never ships a vacuous report. A failed synthesis
preserves the structured results and exits non-zero; failed *workers* warn and continue
with partial coverage.

## Run modes

Two orchestration interfaces, plus a single-agent baseline:

- **`--interface kimi`** (default) — the tool surface Kimi K2.5/K2.6 were RL-trained on
  (K2.5 report, Appendix E.8): `create_subagent(name, system_prompt)` lets the orchestrator
  author each specialist's prompt, then `assign_task(agent, prompt)` dispatches free-text
  tasks that run in parallel. Decomposes by **task** — the sub-agent self-gathers its own
  context with `read_file`/`grep`, as in the paper. (Personas are reusable; each task is a
  fresh agent.)
- **`--interface structured`** — the orchestrator calls `dispatch_workers([{role, focus,
  paths}])` and the harness preloads each worker's files. Decomposes by **scope** (assign
  directories) — simple and deterministic; a good fit when you want tighter control on
  large repos.
- **`--solo`** — one agent, no orchestration, the whole repo in one large context: the
  paper's single-agent baseline. Same verify/synthesize tail, so the outputs are
  shape-identical to a swarm run — point both at the same repo and compare findings,
  latency, tokens, and the critical/total step counts. Size its context with
  `--context-chars` (≈4 chars/token; defaults to 3M).

## Models & providers

`--provider` selects the endpoint and key env var; both serve the Open Responses API
(`/v1/responses`):

| `--provider` | Base URL | API key env |
|--------------|----------|-------------|
| `doubleword` (default) | `https://api.doubleword.ai/v1` | `DOUBLEWORD_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |

`--model` defaults to `moonshotai/Kimi-K2.6` (aliases: `k2.6`, `k2.5`). Aliases are
Doubleword model names — for another provider pass that provider's own id:

```bash
export OPENAI_API_KEY=sk-...
swarm run audit --repo psf/requests --provider openai -m gpt-5.2 --temperature none
```

gpt-5-class reasoning models reject `temperature` and use `reasoning.effort`: pass
`--temperature none` and `--reasoning-effort minimal|low|medium|high|none`. (If a model
rejects a request param the client auto-drops it and retries, so the defaults still work —
being explicit just saves the one extra round-trip.)

## Useful flags

- **Mode & models:** `--interface kimi|structured` (default kimi) · `--solo` · `-m/--model` ·
  `--worker-model` (cheap workers, strong orchestrator/synthesizer).
- **Context budget:** `--context-chars` (per-agent preload, ~4 chars/token) ·
  `--max-output-tokens` — size them to your model's window.
- **Request params:** `--reasoning-effort …` · `--temperature <float>|none`.
- **Tiers:** `--service-tier priority|flex` · `--background/--no-background` ·
  `--max-concurrent` · `--timeout` (per-request seconds; raise for big orchestrator turns).
- **Budgets:** `--max-files` · `--max-agents` · `--max-waves` · `--max-steps` ·
  `--max-rounds` · `--max-files-per-worker`.
- **Verify & search:** `--verify-votes N` (panel size, majority vote) · `--no-verify` ·
  `--enable-search` (else on iff `SERPER_API_KEY` set).
- **Visibility:** `-v` prints a per-call line (role, agent, elapsed, tokens, finish) + the
  dispatch plan; `-vv` adds each agent's tool calls — the fastest way to see which call is
  slow. `--dry-run` prints the plan with no API calls.

The *same* swarm runs realtime (`--service-tier priority`) or async (`--service-tier flex
--background`); `swarm compare <brief> --repo …` runs both and writes a wall-clock / token
/ cost table. Cost is computed from reported token usage — treat `dw usage` as the source
of truth for actual spend.

## Faithful to Kimi

Kimi K2.5/K2.6 are RL-trained ([PARL](https://arxiv.org/html/2602.02276v1)) to be good at
*orchestrating* a swarm — but the spawning, parallel execution, context isolation, and
aggregation are **runtime scaffolding** in Moonshot's hosted product, not in the open
weights. This harness *is* that scaffolding.

**Reproduced:**

- **Self-designing orchestrator** — the model picks the decomposition and team width;
  `--interface kimi` is the literal trained `create_subagent`/`assign_task` surface.
- **Bounded local context + results-only route-back** — the "context sharding" that is the
  swarm's core efficiency claim (workers return findings, never their research).
- **The critical-steps metric** — every run reports critical vs. total steps (the paper's
  parallelism objective), so you see the decomposition the way the paper scores it.

**Dropped, deliberately:**

- **PARL / RL training** — we don't train; we run open weights behind an API. The model
  brings the orchestration skill, we bring the harness.
- **The broad mutating toolbox** (code-interpreter, bash, file-edit, artifacts,
  computer-use, memory) — shipped a non-mutating **read-only v1**, so it's safe to point at
  any repo and reproducible. Write/execute is the v2 roadmap.
- **Non-repo corpora** — repo-only for now (a `Corpus` abstraction is roadmap).

**Ours, not Kimi's:** the per-finding adversarial verifier and the separate synthesizer
(the blog's "reconciliation" is perspective diversity; the paper's orchestrator reconciles
inline), plus the `structured` scope-partition interface and the `--solo` baseline.

## Tools (read-only v1)

Every tool is **non-mutating** — the swarm reads and analyses, never changes the target.
Engine tools: `dispatch_workers` *or* `create_subagent`+`assign_task` (orchestrator),
`submit_results` (worker terminal), `submit_verdict` (verifier terminal); the orchestrator
also gets `read_file`/`grep` to probe. Capability tools a brief grants its workers:

| Tool | Description |
|------|-------------|
| `read_file` | Read a repo file to follow an import/definition |
| `grep` | Regex-search the repo to trace a value to its sink |
| `run_sast` | Run static analysers (bandit/semgrep/…) — read-only |
| `check_advisory` | Look up a dependency's CVEs on OSV (keyless) |
| `web_search` / `read_page` | Ground a finding against docs/advisories (opt-in) |

**Roadmap:** v2 adds write tools — a "fixer" brief that goes *audit → propose patch →
verify patch in a sandbox → open a PR*. Later: a `Corpus` abstraction for non-repo briefs
(web/files), sandboxed `code-interpreter`, persistent memory, browser/computer-use — the
rest of Moonshot's hosted toolbox, shipped in stages.

## Write your own brief

A brief is ~50 lines: prompts, a result schema, a tool selection. Drop a module in
`src/briefs/`, build a `Brief`, `register(...)` it, and `swarm run <name>` works — no engine
changes:

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

## Architecture

```
src/
├── cli.py            # `swarm run <brief>` / report / compare / briefs + results writing
├── engine.py         # the generic swarm loop (brief-agnostic): orchestrate → workers → verify → synthesize
├── responses.py      # spec-clean Open Responses client + dispatch (concurrent | background)
├── cost.py           # per-(model, tier) rate table + cost computation
├── briefs/           # Brief dataclass + registry; audit.py, onboarding.py
└── tools/            # flat tool schemas + execution: repo (clone/list/read/grep), sast, advisory, search
```

`engine.run_swarm(client, brief, root, files, cfg)` is the core — it never mentions audits.
Run the tests with `uv run pytest`: the engine is covered end-to-end with a mocked dispatch
(no network) — both interfaces, the failure paths, schema validation, the forced
submit/verdict turns, the vote panel, context budgets, and step accounting.

## Limitations & notes

- **Reasoning latency:** K2.6 reasons; even at `reasoning.effort=minimal` each call is tens
  of seconds, so a swarm takes minutes. The orchestrator's decomposition turn is the
  heaviest — the repo map is capped and it assigns by directory to keep that turn small.
  `--timeout` (default 600s) bounds a stalled call; raise it for very large repos.
- **Read-only:** results include suggested fixes as text; nothing is applied or executed.
- **Verification is a filter, not a proof:** the panel cuts false positives (and flags what
  it couldn't verify) but doesn't eliminate them; large repos are sampled to `--max-files`.
- **No training:** PARL is how Kimi *trains* the orchestrator; this repo is inference-time
  scaffolding, so parallelization quality rides on the base model.
