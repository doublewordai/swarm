# ADR-001 — opencode as a v2 agent runtime

**Status:** proposed · branch `v2-opencode-runtime`
**Date:** 2026-06

## Questions this answers

1. **Is opencode built for Kimi swarms?** — No.
2. **Can we use it as our agent runtime for v2?** — Yes, via its HTTP server, and the
   seam in our engine is small. This ADR lays that seam and scopes the rest.

## 1. Is opencode built for Kimi swarms?

No — opencode (SST/Anomaly) is a general terminal **coding agent**, not a swarm
framework. It is *not* organised around the PARL parallel-agent paradigm, the
`create_subagent`/`assign_task` schema, or structured-output fan-out. What it *does*
have are the right primitives to host a swarm:

- **primary agents + subagents**, with a built-in **`task` tool** for delegation
  (a primary agent spawns specialised subagents — structurally our orchestrator → workers);
- **custom tools** (description + args + execute) and **MCP** servers;
- built-in `read`/`grep`/`bash`/edit tools and multi-provider routing (incl. local/Ollama);
- a headless **HTTP server** (`opencode serve`) with an **OpenAPI 3.1 spec** at `/doc`,
  and — importantly — **structured output**: `session.prompt(format=<json schema>)`.

So it is *swarm-capable*, not *swarm-shaped*. We would be driving it, supplying the
orchestration logic, the brief abstraction, the verify panel, and synthesis ourselves —
opencode provides the agent runtime underneath, we keep the swarm on top.

## 2. The integration shape

The JS/TS SDK (`@opencode-ai/sdk`) is **TypeScript only — no Python client**. Our codebase
is Python, so we drive opencode over its **HTTP server**, not in-process:

```
swarm (Python)  →  opencode serve (Bun/Hono, :4096)  →  models/tools/subagents
   engine.SwarmConfig.runtime = OpenCodeRuntime(base_url=…)
```

Relevant endpoints (from the OpenAPI spec): `POST /session`, `POST /session/:id/message`
(model + agent + `format` schema), `GET /global/event` (SSE). We'd generate a thin client
from `/doc` or hand-roll `requests` calls.

### Why the seam in our engine is tiny

The engine funnels **every** model call through one indirection (`engine._dispatch`), and
all its parsers (`text_of` / `function_calls_of` / `usage_of` / `finish_of`) operate on
the **Open Responses dict shape**. So a runtime's entire contract is:

> turn a batch of `engine._req(...)` request dicts into Responses-shaped result dicts.

That contract is now the `runtimes.Runtime` protocol. The native `responses` module is the
reference implementation; an opencode runtime is a second one. **This branch implements the
seam** (`SwarmConfig.runtime`, default `None` ⇒ `responses`) and proves it with an injected
fake runtime in the tests — no behavioural change to the existing swarm.

## 3. What an `OpenCodeRuntime` has to do (the real work, deferred)

`dispatch(client, requests, …)` maps each request both ways:

- **request → opencode:** our `input_items` (system/user messages, `function_call`,
  `function_call_output`) → an opencode session + `POST /session/:id/message`; our flat
  `tools` → opencode custom tools / MCP; our `result_schema` → `prompt(format=…)`.
- **opencode → us:** opencode's message **parts** (text, tool calls, tokens, finish) →
  the Responses-shaped dict the engine already parses (`output` items + `usage`).

This is the same kind of translation the (reverted) chat adapter did — except opencode's
**`format` schema support means we can keep schema-enforced `submit_results`**, which a raw
chat-completions backend could not. That materially improves the case over the earlier
Moonshot adapter assessment.

## 4. Open questions (must validate against a live `opencode serve`)

These are undocumented or unverified and gate a real implementation — do **not** build the
client speculatively until they're answered against a running server:

1. **Custom-tool registration over HTTP** — the docs call tools "experimental"; it's unclear
   whether `run_sast`/`check_advisory`/`submit_results` can be registered via the API or only
   via on-disk config/MCP. (MCP is the likely path — wrap our tools as an MCP server.)
2. **Parallelism control** — can we drive opencode's `task` delegation as explicit parallel
   *waves* with our budgets/route-back, or does it own scheduling? Our critical-steps metric
   and `--max-waves` depend on controlling this.
3. **Statelessness vs sessions** — opencode is session/stateful; our engine is stateless
   (replays all items each turn). Do we run one session per agent, or map turns onto session
   messages? Affects how reasoning-item linkage and context budgets behave.
4. **Token usage & timing** — does the message/event payload expose per-call input/output
   (and reasoning) tokens for our per-model cost + `_elapsed_s`?
5. **Read-only safety** — opencode's default Build agent has `bash`/edit/write. We'd pin a
   Plan-style agent + a read-only tool allowlist to preserve v1's non-mutating guarantee.

## 5. Where opencode actually pays off

For the **current read-only swarm**, the native `responses` runtime stays best (full control,
fidelity, metrics). opencode earns its place at **v2 auto-fix**: write/edit/bash, a permission
system, sessions, sandboxing, and a TUI all ship out of the box — exactly the "fixer" toolchain
(audit → patch → sandboxed test → PR) we'd otherwise build from scratch. So the strategic read
is: **opencode is the runtime for the *mutating* v2, riding the seam we just added**, while the
read-only swarm keeps the Responses backend.

## Decision

- **Now (this branch):** ship the runtime seam (`runtimes.Runtime` + `SwarmConfig.runtime`),
  default unchanged, tested. Low-risk, independently useful, and the prerequisite for any
  backend swap.
- **Next (gated on the §4 answers):** spin up `opencode serve`, expose our tools as an MCP
  server, and build `OpenCodeRuntime.dispatch` against the live OpenAPI — validating the
  translation on one brief (`audit`) end-to-end before wiring `--runtime opencode` into the CLI.
- **Not now:** a speculative HTTP client written without a server to test against (that was the
  Moonshot-adapter mistake).
