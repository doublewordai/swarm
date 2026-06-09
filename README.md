# Kimi Agent Swarm: a self-designing code auditor

A worked example of how to **execute a Kimi agent swarm** on the
[Doubleword](https://doubleword.ai) inference server using the
[Open Responses API](https://openresponses.org).

Point it at any codebase and an LLM **orchestrator designs its own audit team**:
it decomposes the repo into parallel subtasks, spawns **bounded-context worker
agents** (each sees only its assigned files), an **adversarial verifier** stage
challenges every candidate finding, and a **synthesizer** writes the report.

The model is a runtime parameter (default `moonshotai/Kimi-K2.6`), and every
agent call can run on either service tier — `priority` (realtime) or `flex`
(async, `background`+poll) — so you can drive your own cost/latency comparison
across tiers and models with one flag.

To run it: install the [dw CLI](https://github.com/doublewordai/dw) and `dw login`,
or sign up at [app.doubleword.ai](https://app.doubleword.ai).

## How the swarm works

```
audit("owner/repo")
        │
   Repo map (code): shallow-clone → filter source files → compact tree + headers
        │   "map-first": the orchestrator can decompose immediately, no wasted round
        ▼
   Orchestrator (LLM "CEO"): decomposes the audit + designs the team — it picks the
        strategy (by-file · by-subsystem · by-security-concern · hybrid) AND the width
        │   tool: dispatch_workers([{role, focus, files}])     ← dynamic delegation
        ├─ Worker 0 [injection]    scope: handlers/*, db.py   ─┐ bounded local context:
        ├─ Worker 1 [authz]        scope: auth/*               │ only its slice pre-loaded,
        ├─ Worker 2 [concurrency]  scope: worker_pool.py       │ own memory — ONLY findings
        └─ Worker K [secrets/deps] scope: config/*            ─┘ route back to orchestrator
              │   worker tools: read_file, grep, report_findings
              ▼
   Dedupe (code) → candidate findings
        │   anti-groupthink: each finding challenged independently before reconciliation
        ├─ Verifier 0 finding A  ─┐  skeptic: tries to REFUTE — reachable? real bug? FP?
        ├─ Verifier 1 finding B   │
        └─ Verifier M finding Z  ─┘  → confirm/refute + adjusted severity + reasoning
              ▼
   Synthesizer (1 call): reconcile confirmed findings → report.md + findings.json
```

A real run on this repo's own `src/` shows the behavior well: with no hand-holding,
the orchestrator spun up an `injection-filesystem` worker on `cli.py` and a
`logic-secrets-validation` worker across `cli.py`/`cost.py`/`__init__.py`, each
with a sharp focus, then synthesized a clean triaged report.

### Reimplementing Kimi's agent swarm on open weights

Kimi K2.5/K2.6 are RL-trained ([PARL](https://arxiv.org/html/2602.02276v1)) to be
*good at orchestrating* a swarm — deciding decomposition, parallel width, and how to
merge. But the spawning, parallel execution, context isolation, and aggregation are
**runtime scaffolding**, which lives in Moonshot's hosted Agent-Swarm product, not in
the open weights. On Doubleword we have the raw model behind the Open Responses API,
so this harness *is* that scaffolding. It reproduces four principles of Kimi's design:

1. **Self-designing orchestrator** — the model chooses the team and the decomposition.
2. **Bounded local context + route-back** — workers are semantically isolated and
   return only findings (proactive context management; also keeps per-agent tokens low).
3. **Structural anti-groupthink** — independent verifiers refute before reconciliation.
4. **Synthesis** — one pass reconciles the confirmed findings.

### Tools

| Tool | Role | Description | Execution |
|------|------|-------------|-----------|
| `dispatch_workers` | orchestrator | Spawn parallel workers, each scoped to specific files | **Deferred** (orchestrator waits) |
| `read_file` | worker | Read a repo file to follow an import/definition | Immediate |
| `grep` | worker | Regex-search the repo to trace a value to its sink | Immediate |
| `report_findings` | worker | Submit findings and finish | **Deferred** (terminal) |
| `submit_verdict` | verifier | Confirm or refute one finding | **Deferred** (terminal) |

Built spec-clean against the Open Responses API: flat function tools, caller-owned
`input` items, `background`+poll for async, `service_tier` for tier selection, and
`reasoning.effort` to control how much the model "thinks" (default `minimal` keeps
reasoning models like K2.6 responsive). No provider-specific workarounds.

## Running it

```bash
dw login
dw examples clone kimi-swarm-audit
cd kimi-swarm-audit
dw project setup
```

Audit a public repo:

```bash
dw project run audit -- --repo psf/requests --max-files 20
```

Audit a local checkout:

```bash
dw project run audit -- --path ./my-service
```

View the latest report:

```bash
dw project run report
```

Quick look without spending tokens (prints the repo map + the orchestrator's plan):

```bash
dw project run audit -- --path ./my-service --dry-run
```

### The model is a runtime parameter

`--model` defaults to `moonshotai/Kimi-K2.6` and accepts an alias (`k2.6`, `k2.5`)
or any full `model_name` Doubleword serves. "Kimi swarm" is the default flavor, not
a hard dependency:

```bash
dw project run audit -- --repo psf/requests -m k2.5
dw project run audit -- --repo psf/requests -m moonshotai/Kimi-K2.6
```

### Useful flags

`--service-tier priority|flex` · `--background/--no-background` · `--max-files`
(skipped files are logged) · `--max-agents` · `--max-waves` · `--max-rounds` ·
`--no-verify` · `--dry-run` · `-o/--output`.

## Service tiers & measuring cost

Every agent call runs on the tier you choose, so the *same* swarm can run two ways:

- **`--service-tier priority`** — realtime; calls fire concurrently and block.
- **`--service-tier flex --background`** — async; the whole swarm is submitted as
  background jobs and polled.

`compare` runs the identical audit through both tiers and writes a wall-clock /
token / cost table to `results/<slug>/analysis.md`:

```bash
dw project run compare -- --repo psf/requests --max-files 20
```

See [`analysis.md`](analysis.md) for how to read the comparison and measure actual
cost — note that whether `flex` is cheaper than `priority` depends on the model's
configured tier pricing, so point `compare` at the model you'll actually use.

## Architecture

```
src/
├── cli.py          # audit / report / compare commands + results writing
├── swarm.py        # roles, orchestration loop, bounded-context builders, accounting
├── responses.py    # spec-clean Open Responses client + dispatch (concurrent | background)
├── cost.py         # per-(model, tier) rate table + cost computation
├── prompts.py      # orchestrator / worker / verifier / synthesis prompts
└── tools/
    ├── __init__.py # flat tool schemas + execute_tool dispatch
    └── repo.py     # clone / list / filter / read / grep + repo map
```

`swarm.py` is the core. `run_audit` drives the orchestrator turn-by-turn and runs
each wave of workers and verifiers through `responses.dispatch`. The realtime/async
behavior lives entirely in `dispatch`, so the engine is tier-agnostic. Results land
in `results/<repo-slug>/{report.md, findings.json, swarm-tree.json, summary.json}`.

Run the tests with `uv run pytest` — the engine is covered end-to-end with a mocked
dispatch (no network), plus unit tests for the client, repo tools, schemas, and cost.

## Limitations & notes

- **Reasoning latency:** K2.6 is a reasoning model; even at `reasoning.effort=minimal`
  each call is tens of seconds, so a swarm takes minutes. The client uses a request
  timeout and fails a stalled call gracefully rather than hanging the run.
- **Cost figures are a guide:** the in-tool cost is computed from the API's reported
  token usage; treat `dw usage` as the source of truth for actual spend.
- **Read-only:** findings include suggested fixes as text; nothing is applied or run.
  Reachability is reasoned, not proven by executing the code.
- Large repos are sampled to `--max-files` (skipped files are logged); the verifier
  stage reduces false positives but does not eliminate them.
