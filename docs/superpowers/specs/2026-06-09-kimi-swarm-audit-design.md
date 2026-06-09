# Kimi Swarm Audit — Design Spec

**Date:** 2026-06-09
**Status:** Approved (design); pending spec review
**Author:** Peter (peter@doubleword.ai) + Claude

## Summary

A new Doubleword example, sibling to `async-agents`, that runs a **Kimi agent
swarm** to **audit any codebase for bugs and security issues**. It is built on
the **Open Responses API** (`/v1/responses`) and is designed to run two ways
from the same harness:

- **Realtime** — `service_tier="priority"`, blocking calls fired concurrently.
- **Async** — `service_tier="flex"` + `background=True`, submit the whole swarm
  as background jobs, poll, collect.

The point of the example is to show the **cost ↔ latency tradeoff** between the
two tiers on a genuinely agentic, embarrassingly-parallel workload, while
reproducing **Kimi's agent-swarm execution model** on open weights.

This document is the design contract for the implementation plan that follows.

## Motivation

Agentic audits are many-call workloads: an orchestrator plans, N workers each
read code and reason over it, M verifiers challenge each finding, and a
synthesizer reconciles. At realtime rates that is expensive; at flex/batch rates
it is cheap but each round adds latency. Doubleword exposes both tiers behind one
OpenAI-compatible endpoint, so a single harness can run the *same* swarm both
ways and produce an honest comparison.

We choose a **code bug-hunt / security audit** because it (a) is embarrassingly
parallel (one worker per file/subsystem), (b) plays to Kimi K2.5/K2.6's
coding strength, (c) produces tangible, verifiable output, and (d) is
reproducible by anyone against any public repo.

## Relationship to Kimi's agent swarm

We reimplement Kimi's *philosophy*, not any Kimi-specific API. On Doubleword we
have only the raw open-weight model behind `/v1/responses`; there is no
Moonshot agent runtime in the path, so the orchestration scaffolding is ours.
Four principles from Kimi K2.5/K2.6 (PARL) shape the design:

1. **Self-designing orchestrator.** An LLM "CEO" decomposes the audit into
   parallel subtasks and *chooses the strategy itself* — by-file, by-subsystem,
   by-security-concern, or hybrid — and the parallel width. Not hardcoded.
2. **Bounded local context + route-back.** Each worker gets a semantically
   isolated slice (only its files pre-loaded), its own working memory, and
   returns **only its findings** to the orchestrator — never its full scratch
   context. Kimi frames the swarm as *proactive context management*.
3. **Structural anti-groupthink.** Independent verifiers challenge each finding
   *before* reconciliation, rather than one model agreeing with itself.
4. **Synthesizer reconciles.** A final pass merges confirmed findings into one
   report.

**Key synergy:** principle (2) — bounded context, return only findings — is the
*same* mechanism that keeps per-agent input tokens low, which is exactly what
makes the flex/async cost story dramatic. Context integrity and cost savings are
the same design lever. This is the throughline of the demo.

## Goals

- Reproduce Kimi's swarm topology (orchestrator → isolated workers → verifiers →
  synthesizer) on open weights via Open Responses.
- Run the identical swarm in **realtime** and **async** tiers and measure
  wall-clock, tokens, and cost for each.
- Ship in the `async-agents` style: self-contained, cloneable, `dw project run`.
- Produce a triaged, verifiable audit report for any public repo or local path.

## Non-goals

- No model training / RL. We are inference-only; the model's trained
  orchestration propensity does the "designing."
- No write/fix actions on the target repo. Read-only audit (findings + suggested
  fixes as text). No code execution / sandboxing of the target.
- No arbitrary recursion. The swarm is **one level** (orchestrator → workers),
  unlike `async-agents`' recursive tree. Verifiers are a separate flat stage.
- No reuse of `async-agents`' Doubleword-specific Open Responses workarounds
  (see "Open Responses usage").

## Architecture

```
kimi-swarm-audit/
├── README.md              # narrative + cost tables (async-agents style)
├── analysis.md            # realtime vs async comparison (curated from compare runs)
├── dw.toml                # steps: audit, report, compare
├── pyproject.toml         # deps: click, openai
├── docs/superpowers/specs/2026-06-09-kimi-swarm-audit-design.md
├── src/
│   ├── __init__.py
│   ├── cli.py             # commands (audit, report, compare) + results writing
│   ├── swarm.py           # swarm engine: roles, orchestration loop, state, accounting
│   ├── responses.py       # spec-clean Open Responses client + dispatch (concurrent/background)
│   ├── cost.py            # per-(model,tier) rate table + cost computation
│   ├── prompts.py         # ORCHESTRATOR / WORKER / VERIFIER / SYNTHESIS system prompts
│   └── tools/
│       ├── __init__.py    # flat tool schemas + local execution dispatch
│       └── repo.py        # clone / list / filter / read / grep the target repo
└── results/<repo-slug>/
    ├── report.md          # triaged human-readable audit
    ├── findings.json      # machine-readable findings (+ verdicts)
    ├── swarm-tree.json    # agents, roles, scopes, status, rounds
    └── summary.json       # model, tier, background, tokens, wall-clock, cost, coverage
```

Each module has one job and a narrow interface, so it can be understood and
tested on its own:

- **`responses.py`** — *only* speaks Open Responses. No domain logic.
- **`tools/repo.py`** — *only* filesystem/git over the target repo.
- **`tools/__init__.py`** — tool *schemas* + a pure `execute_tool(name, args)`.
- **`swarm.py`** — the engine; depends on the three above + prompts.
- **`cost.py`** — pure functions over a rate table.
- **`cli.py`** — wiring, flags, output files; no swarm logic of its own.

### Swarm topology

```
audit("owner/repo")
        │
   Repo map (repo.py): shallow git clone → filter source files → compact tree
        │   + sizes + first ~40 lines (headers) of each file.  "map-first":
        │   the orchestrator can decompose immediately, no wasted round.
        ▼
   Orchestrator (LLM, role=orchestrator):
        sees the repo map; calls dispatch_workers([{role, focus, files}, …]),
        choosing strategy (by-file | by-subsystem | by-concern | hybrid) and width.
        │
        ├─ Worker 0 [injection]    files: handlers/*, db.py     ─┐ bounded context:
        ├─ Worker 1 [authz]        files: auth/*                 │ ONLY its files
        ├─ Worker 2 [concurrency]  files: worker_pool.py         │ pre-loaded; own
        └─ Worker K [secrets/deps] files: config/*, lockfiles   ─┘ memory; returns
              │   tools: read_file, grep, report_findings           ONLY findings
              ▼
   (orchestrator may dispatch a 2nd wave to cover gaps, up to --max-waves)
        ▼
   Dedupe (swarm.py): merge candidate findings by (file, line-ish, title)
        │
        ├─ Verifier 0 finding A  ─┐  adversarial: try to REFUTE — reachable? real?
        ├─ Verifier 1 finding B   │  false positive?   one verifier per finding,
        └─ Verifier M finding Z  ─┘  dispatched in parallel (anti-groupthink)
              │   tool: submit_verdict({is_real, confidence, adjusted_severity, reasoning})
              ▼
   Keep confirmed (swarm.py) → Synthesis turn (LLM, tool-free):
        reconcile confirmed findings → report.md (+ findings.json)
```

## Open Responses usage (spec-clean, no Doubleword workarounds)

We code against the Open Responses spec and **trust it**. We do *not* port the
`async-agents` band-aids (the wrapped-tool shape it sent because flat tools used
to 422). If `/v1/responses` deviates from spec, we surface it as a Doubleword
bug rather than papering over it.

- **Endpoint:** `POST /v1/responses`; `client.responses.create(...)`,
  `client.responses.retrieve(id)`.
- **Tools:** *flat* function defs —
  `{"type":"function","name":..., "description":..., "parameters":{…json schema…}}`.
  (Not the chat-completions `{"type":"function","function":{…}}` wrapper.)
- **Input:** we own the `input` item list per agent (precise bounded-context
  control). Item types we emit:
  - `{"type":"message","role":"system|user|assistant","content": "<text>"}`
  - `{"type":"function_call","call_id","name","arguments"}` (echoing the model's
    prior calls back as history)
  - `{"type":"function_call_output","call_id","output":"<json string>"}`
- **Output parsing:** read `response.output[]` items —
  `message` (→ `content[].output_text`), `function_call` (`call_id/name/arguments`),
  `reasoning` (ignored downstream). `response.output_text` is the text convenience.
- **Usage/cost:** read `response.usage` → `input_tokens`, `output_tokens`,
  `total_tokens`.
- **Tiers:** `service_tier="priority"` (realtime) vs `"flex"` (async).
- **Realtime dispatch:** `background=False`; fire all ready agents concurrently
  via a thread pool; each call blocks until done.
- **Async dispatch:** `background=True`; `create` returns `{id, status:"queued"}`
  immediately; submit all ready agents, then poll `retrieve(id)` until
  `status ∈ {completed, failed, incomplete, cancelled}`; collect.
- **Optional optimization:** `previous_response_id` to keep an agent's context
  server-side across its own rounds (cuts payload). v1 keeps explicit input
  lists for clarity; previous_response_id is a follow-up.

### `responses.py` interface (sketch)

```python
TERMINAL = {"completed", "failed", "incomplete", "cancelled"}

def make_client(provider="doubleword") -> OpenAI            # base_url + API key from env
def call(client, *, model, input_items, tools=None, tool_choice=None,
         service_tier="priority", background=False,
         max_output_tokens=8192, temperature=0) -> Response  # one turn
def poll(client, response_id, interval=3.0, timeout=...) -> Response
def dispatch(client, requests, *, service_tier, background) -> list[Response]
    # requests: list of dicts (model, input_items, tools, ...). Blocking →
    # ThreadPool(create). Background → create all, then poll all. Failures map
    # to a failed Response, never raise out of dispatch.

# pure parse helpers
def text_of(resp) -> str
def function_calls_of(resp) -> list[dict]   # [{call_id, name, arguments}]
def usage_of(resp) -> dict                  # {input_tokens, output_tokens}
def finish_of(resp) -> str                  # "stop" | "tool_calls" | "length" | "error"
```

Transient errors (timeouts, connection, 429) retry with backoff (mirrors
`async-agents`' `_with_retries`, reimplemented locally). No XML tool-call
fallback unless the served Kimi model proves to need it (decide at build).

## Tools (flat schemas)

Orchestrator:

- **`dispatch_workers(workers: [{role: str, focus: str, files: [str]}])`** —
  *deferred*: the engine creates one worker per entry, pauses the orchestrator
  until all workers complete, then returns the compiled findings. `role`/`focus`
  set the worker's lens; `files` is its bounded scope.

Worker:

- **`read_file(path: str)`** — return file contents (capped, e.g. 60k chars).
  For following an import or reading a file outside its pre-loaded slice.
- **`grep(pattern: str, path?: str)`** — regex search across the repo
  (capped hits). For tracing a tainted value / sink across files.
- **`report_findings(findings: [{severity, title, file, line, description,
  suggested_fix, confidence}])`** — *terminal*: records findings and completes
  the worker. `severity ∈ {critical, high, medium, low, info}`,
  `confidence ∈ [0,1]`.

Verifier:

- **`submit_verdict({is_real: bool, confidence: float, adjusted_severity: str,
  reasoning: str})`** — *terminal*: records the verdict for one candidate finding.

`execute_tool(name, arguments)` handles `read_file`/`grep` immediately and
returns a `DEFERRED` sentinel for `dispatch_workers`/`report_findings`/
`submit_verdict`, which the engine resolves (matches the `async-agents`
deferred-tool pattern, but only one level deep).

## Orchestration loop (`swarm.py`)

Roles: `orchestrator`, `worker`, `verifier`. One `SwarmRun` owns the agents,
the dispatch tier/background flags, and token/cost/wall-clock accounting.

1. **Map.** `repo.py` clones/filters → repo map. Build the orchestrator agent
   with the map pre-loaded (map-first).
2. **Plan.** Dispatch the orchestrator. It calls `dispatch_workers(...)`.
   The engine creates workers, each with a **bounded** input: system prompt for
   its `role`/`focus`, plus only its `files` pre-loaded (read-first). Orchestrator
   → `waiting_for_workers`.
3. **Find.** Dispatch all ready workers (realtime: concurrent; async:
   background+poll). Workers may `read_file`/`grep` (extra rounds, capped by
   `--max-rounds`) then `report_findings`. Only findings route back.
4. **Resolve.** When all of a wave's workers finish, compile their findings into
   the `dispatch_workers` tool result and resume the orchestrator.
5. **(Optional) gap wave.** Orchestrator may `dispatch_workers` again
   (up to `--max-waves`, default 2) or stop.
6. **Dedupe.** Merge candidate findings (by file + nearby line + normalized
   title).
7. **Verify.** Create one verifier per deduped finding, each with the finding +
   the relevant code pre-loaded; dispatch (same tier machinery); collect verdicts
   (anti-groupthink). Skippable via `--no-verify`.
8. **Keep** findings whose verdict `is_real` (or all, if `--no-verify`),
   applying `adjusted_severity`.
9. **Synthesize.** A tool-free orchestrator turn given the confirmed findings →
   `report.md`. (Same trick as `async-agents`' final report turn.)

State per agent: `pending → in_progress → (waiting_for_workers) → completed |
failed`. A failed worker/verifier is recorded and skipped; it never blocks the
wave. Safety bounds: `--max-rounds` per agent, `--max-agents` width,
`--max-turns` global.

## Realtime vs async comparison

`compare` runs the **same** repo through both tiers and writes `analysis.md`:

| Tier | service_tier | background | Input tok | Output tok | Wall-clock | Cost |
|------|--------------|-----------|-----------|-----------|------------|------|

- **Tokens** are *measured* from `usage` (never estimated).
- **Wall-clock** is measured around the whole run.
- **Cost** = measured tokens × a documented per-(model, tier) rate table in
  `cost.py`.

### `cost.py` rate table (build-time-verified inputs)

Rates live in one dict so they are trivial to update; the harness only ever
*measures* tokens, so cost stays honest if rates change.

```python
# $ per 1M tokens (input, output). VERIFY at build via /v1/models + `dw usage`.
RATES = {
  "<doubleword-kimi-id>": {
    "priority": (IN_RT,  OUT_RT),   # realtime
    "flex":     (IN_FLEX, OUT_FLEX) # async (priced below realtime, above batch)
    # "batch":  (IN_B, OUT_B)       # 24h Batch API, for reference in README
  },
}
```

Starting reference points to verify (not assumed final): Moonshot list price for
K2.6 is ≈ $0.95 in / $4.00 out realtime, with batch ≈ 60% of realtime; Doubleword
exposes lower-priority tiers (flex/batch) as in the `async-agents` analysis. The
exact Doubleword Kimi id + tier rates are confirmed in the first build step.

## CLI & dw.toml

```
audit   --repo owner/name | --path DIR
        -m, --model <alias|id>          # default: Doubleword's served Kimi
        --service-tier priority|flex    # default: priority
        --background / --no-background   # default follows tier (flex→on)
        --max-files N (40)  --max-agents N (12)  --max-waves N (2)
        --max-rounds N (3)  --no-verify  -o results/
report  -o results/                     # print latest report + summary
compare --repo/--path …                 # run both tiers → analysis.md
```

`dw.toml` steps: `audit`, `report`, `compare`, mirroring `async-agents`.
`pyproject.toml`: `kimi-swarm-audit = "src.cli:main"`, deps `click`, `openai`.
Auth via `DOUBLEWORD_API_KEY`; base URL `https://api.doubleword.ai/v1`.

## Guardrails

- **File filtering:** include common source extensions; skip binaries,
  lockfiles, minified/generated files, and vendored dirs (`node_modules`,
  `.venv`, `dist`, `build`, `vendor`, `third_party`, `.git`).
- **No silent caps:** if the repo exceeds `--max-files`, log exactly which files
  were dropped. The orchestrator is given the (possibly capped) file list and
  must assign every listed file to a worker or mark it skipped-with-reason;
  `summary.json` records coverage (assigned / total).
- **Width cap:** `dispatch_workers` is clamped to `--max-agents`; excess is
  logged.
- **Determinism:** `temperature=0` for workers/verifiers/synthesis; the
  orchestrator may run slightly warmer to diversify decomposition (decide at
  build).

## Testing strategy

- **Unit (no network):** `repo.py` filtering/listing/grep on a fixture repo;
  `tools/__init__.py` `execute_tool` dispatch incl. `DEFERRED`; `responses.py`
  parse helpers (`text_of`/`function_calls_of`/`usage_of`) against recorded
  Response fixtures; `cost.py` math; `swarm.py` dedupe.
- **Engine (mocked client):** a fake `dispatch` returning scripted Responses to
  drive a full orchestrator→workers→verify→synthesize run, asserting state
  transitions, bounded-context construction, and findings flow — no API calls.
- **Smoke (live, manual):** `audit` a tiny known repo at `priority`, then
  `compare`, eyeball `report.md`/`analysis.md`. Used to confirm spec-conformance
  of `/v1/responses` and finalize rates.

## Open items (resolved at build, first)

1. **Model id + tier rates.** Query `GET /v1/models` and `dw` to confirm which
   Kimi (K2.5 / K2.6 / -thinking) Doubleword serves and the priority/flex/batch
   rates; seed `cost.py`.
2. **`/v1/responses` conformance.** Confirm flat tools + background polling work
   per spec on Doubleword; if not, file a bug (do not work around).
3. **Tool-call format.** Confirm the served Kimi emits structured
   `function_call` items (no XML fallback needed).
4. **Orchestrator temperature** for decomposition diversity.

## Limitations

- Background/flex rounds add minutes-scale latency per round; the swarm is wide
  and shallow (≤ `--max-waves` + a verify stage) to keep round count low.
- Read-only: findings include suggested fixes as text but nothing is applied or
  executed; reachability is reasoned, not proven by running the code.
- Very large repos are sampled to `--max-files` (logged); not a whole-monorepo
  audit in one run.
- False positives are reduced by the verifier stage but not eliminated.
