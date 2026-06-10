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
        │   degrades (headers → tree → truncated) to fit the orchestrator's context budget
        ▼
   Orchestrator (LLM): decomposes the task + designs the team — picks strategy and width
        │   itself; assigns work by directory (paths, expanded engine-side) so a 500-file
        │   repo is a handful of lines, not 500; can read_file/grep to probe first.
        ├─ Worker 0  scope: …  ─┐ bounded local context: its files pre-loaded (within a
        ├─ Worker 1  scope: …   │ char budget; the rest listed as fetchable), own memory —
        └─ Worker K  scope: …  ─┘ returns ONLY schema-valid results. A forced final submit
        │                         turn means an out-of-budget worker never loses its work.
        ▼   route-back: per-worker status + unreported files → orchestrator may fill gaps
   Dedupe → (optional) Verifier panel: N independent skeptics per item (majority vote),
        │   each with its own tool loop. confirmed → kept · refuted → dropped (counted) ·
        │   no verdict → kept but flagged "unverified" (never silently discarded).
        ▼
   Synthesizer (1 call): reconcile confirmed + unverified → report.md + <results>.json
```

The loop is identical for every brief. A **brief** plugs in the prompts (orchestrator /
worker / verifier / synthesis), the **result schema** the workers emit (enforced —
invalid items are dropped, not trusted), which **tools** each role gets, and the
dedupe/verify hooks. `audit` emits findings and verifies them; `onboarding` emits doc
sections and skips verification.

### Two orchestration interfaces

`--interface structured` (default) — the orchestrator calls `dispatch_workers([{role,
focus, files}])` and the harness builds each worker's bounded context. Simple and
predictable.

`--interface kimi` — the interface Kimi K2.5/K2.6 were **RL-trained on** (K2.5 tech
report, Appendix E.8), kept **schema-faithful**: `create_subagent(name, system_prompt)`
lets the orchestrator **author each specialist's system prompt** and register it for
reuse, then `assign_task(agent, prompt)` dispatches a free-text task — multiple calls in
one turn run in parallel. As in the paper, the sub-agent then **gathers its own context**
(it's given the repo's file listing and uses `read_file`/`grep` to read what its task
needs) rather than receiving harness-assigned files. The brief's result schema and submit
contract are appended to the model-authored prompt, so output stays structured. This is
the literal trained tool surface (`{agent, prompt}`, no extra fields), so K2.5/K2.6 run on
their PARL prior; the harness still enforces context bounds and schema validation.

The two interfaces embody the paper's two faithful-vs-pragmatic readings: `kimi`
decomposes by **task** (sub-agent self-discovers scope, as trained); `structured`
decomposes by **scope** (the orchestrator assigns directories, the harness pre-loads) —
faster and more deterministic for large code repos, at the cost of matching the trained
schema exactly.

### Single-agent baseline (`--solo`)

`--solo` is the paper's comparison point: **one** agent, no orchestration, the whole repo
in one large context (preloaded up to a big budget, the rest reachable via
`read_file`/`grep`), emitting all findings in one `submit_results`. The verify and
synthesize stages are unchanged, so `findings.json` / `report.md` are byte-identical in
shape to a swarm run — the *only* variable is orchestration + parallelism. Point both at
the same repo and compare findings, latency, tokens, and the critical/total step counts
to see what the swarm buys you (the paper reports single-agent 60.6 → swarm 78.4 on
BrowseComp, 3–4.5× faster). Add `--no-verify` for a literally-one-agent run; note that on
a repo larger than the context window the solo agent leans on `read_file` for the tail,
and a whole-repo audit can bump the output-token ceiling.

Size the budget to your model with `--context-chars` (preload budget, ~4 chars/token) and
`--max-output-tokens`; `--solo` defaults to 3M chars / 32k output, but e.g. a 256k-context
model wants `--context-chars 800000`, a 1M model `--context-chars 3500000`. Both flags work
in swarm mode too (they set the per-worker budget). Programmatically it's just
`SwarmConfig(solo=True, worker_context_chars=…, worker_max_output_tokens=…)` — the CLI
numbers are only defaults.

**Failure is loud.** A dead orchestrator call, or a run that dispatches zero workers,
raises `SwarmError` and exits non-zero — it never ships a vacuous report. A failed
synthesis writes the structured results and exits non-zero with a clear message; failed
*workers* are surfaced as warnings (the run continues with partial coverage).

### Reimplementing Kimi's agent swarm on open weights

Kimi K2.5/K2.6 are RL-trained ([PARL](https://arxiv.org/html/2602.02276v1)) to be *good
at orchestrating* a swarm, but the spawning, parallel execution, context isolation, and
aggregation are **runtime scaffolding** that lives in Moonshot's hosted product, not in
the open weights. On Doubleword we have the raw model behind the Open Responses API, so
this harness *is* that scaffolding. It reproduces four principles:

1. **Self-designing orchestrator** — the model chooses the team and the decomposition,
   and (`--interface kimi`) authors each sub-agent's system prompt via the trained
   `create_subagent`/`assign_task` interface.
2. **Bounded local context + route-back** — workers are isolated and return only
   schema-valid results; per-worker status routes back so the orchestrator can fill gaps.
   In `structured` mode the harness pre-loads each worker's file slice (within a context
   budget); in `kimi` mode the worker self-gathers via `read_file`/`grep`, as in the paper.
3. **Structural anti-groupthink** — an independent verifier panel (majority vote) refutes
   before reconciliation; unverified items are flagged, not dropped.
4. **Synthesis** — one pass reconciles confirmed and unverified results.

The orchestration *scaffolding* is ours; the verify/synthesis stages are additions on top
of Kimi's design (the blog's "independent agents → forced reconciliation" is about
perspective diversity, and the paper's orchestrator reconciles inline). What's faithful to
the trained model is the `--interface kimi` tool surface and the bounded-context sharding.

> **On parallelism (PARL):** the paper's orchestrator is RL-trained to decide *whether,
> when, and how* to parallelize, optimizing a *critical-steps* objective (Σ over stages of
> `main + max(sub)` steps). We don't train — but we **measure** it: every run reports
> critical vs. total steps and the implied parallel speedup, so you can see the swarm's
> decomposition the way the paper scores it.

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

Engine tools (every brief): `dispatch_workers` *or* `create_subagent`+`assign_task`
(orchestrator, per `--interface`), `submit_results` (worker terminal — schema is the
brief's), `submit_verdict` (verifier terminal). The orchestrator also gets `read_file` and
`grep` in both interfaces, so it can probe the repo before and between dispatches.
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

### Provider & model are runtime parameters

`--provider` (default `doubleword`) selects the API endpoint and key env var; every
provider must serve the Open Responses API (`/v1/responses`):

| `--provider` | Base URL | API key env |
|--------------|----------|-------------|
| `doubleword` | `https://api.doubleword.ai/v1` | `DOUBLEWORD_API_KEY` |
| `openai`     | `https://api.openai.com/v1`    | `OPENAI_API_KEY` |

`--model` defaults to `moonshotai/Kimi-K2.6` and accepts an alias (`k2.6`, `k2.5`) or any
full `model_name`. **Aliases are Doubleword model names**; with another provider pass that
provider's own id (e.g. `--provider openai -m gpt-5.2`) — the CLI requires an explicit
`-m` there rather than sending a Doubleword alias to the wrong endpoint.

```bash
export OPENAI_API_KEY=sk-...
swarm run audit --repo psf/requests --provider openai -m gpt-5.2 --temperature none
```

OpenAI's gpt-5-class reasoning models reject `temperature` and use `reasoning.effort`;
`--temperature none` omits the parameter, and `--reasoning-effort minimal|low|medium|high`
(or `none` to omit) sets the depth. Both apply to every role (orchestrator, workers,
verifiers, synthesizer).

You don't have to get this exactly right up front: if a model rejects a request param
as unsupported (e.g. `temperature` on gpt-5.5), the client drops that param and retries,
then remembers it for the rest of the run — so the swarm works even with the default
`temperature`. Being explicit (`--temperature none`) just avoids the one extra round-trip
on the first call.

### Useful flags

- **Provider & models:** `--provider doubleword|openai` · `-m/--model` ·
  `--worker-model` (cheap workers, strong orchestrator/synthesizer — the runtime analogue
  of the paper's "train the orchestrator with small sub-agents first").
- **Mode:** `--interface structured|kimi` · `--solo` (single-agent baseline, no
  orchestration; ignores `--interface`).
- **Context budget:** `--context-chars` (per-agent preload, ~4 chars/token) ·
  `--max-output-tokens` (per worker/verifier turn) — size them to your model's window.
- **Request params:** `--reasoning-effort minimal|low|medium|high|none` ·
  `--temperature <float>|none` (omit for models that reject it).
- **Tiers:** `--service-tier priority|flex` · `--background/--no-background` ·
  `--max-concurrent` (in-flight requests per dispatch) · `--timeout` (per-request
  seconds; raise for very large orchestrator turns).
- **Budgets:** `--max-files` (skipped files logged) · `--max-agents` (total workers) ·
  `--max-waves` · `--max-steps` (orchestrator turns) · `--max-rounds` (tool rounds per
  worker/verifier) · `--max-files-per-worker` (oversized specs split engine-side).
- **Verify & search:** `--verify-votes N` (panel size, majority vote) · `--no-verify` ·
  `--enable-search` (else on iff `SERPER_API_KEY` set).
- **Logging:** `-v` shows a per-call line (role, agent, elapsed, tokens, finish) plus the
  orchestrator's dispatch plan and any failed call live; `-vv` adds each agent's tool
  calls. This is the fastest way to see *which* call is slow (e.g. the orchestrator turn).
- `--dry-run` — print the plan (resolved tools per role, repo map) with no API calls.

Per-model token usage, cost (summed across orchestrator/worker models), coverage, and the
critical/total step counts land in `summary.json`.

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
covered end-to-end with a mocked dispatch (no network): both interfaces, the failure
paths (dead orchestrator, zero workers, dead synthesis), schema validation, the forced
submit/verdict turns, the vote panel, context budgets, and the step accounting.

## Limitations & notes

- **Reasoning latency:** K2.6 reasons; even at `reasoning.effort=minimal` each call is
  tens of seconds, so a swarm takes minutes. The orchestrator's decomposition turn is the
  heaviest call — the repo map is capped (`map_max_chars`, big repos go tree-only) and the
  orchestrator assigns by directory rather than enumerating files, to keep that turn small.
  `--timeout` (default 600s) bounds a stalled call; raise it for very large repos.
- **Cost figures are a guide:** computed from the API's reported token usage (per model);
  treat `dw usage` as the source of truth for actual spend.
- **Read-only:** results include suggestions as text; nothing is applied or executed.
- **Verification is a filter, not a proof:** the panel reduces false positives (and flags
  what it couldn't verify) but doesn't eliminate them; large repos are still sampled to
  `--max-files` (skipped files logged).
- **No training:** PARL is how Kimi *trains* the orchestrator; this repo is inference-time
  scaffolding. The `kimi` interface matches the trained tool surface and we report the
  paper's critical-steps metric, but parallelization quality rides on the base model.
