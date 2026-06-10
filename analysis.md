# Comparing service tiers with this tool

The same Doubleword agent swarm runs on either service tier, so you can measure the
cost/latency tradeoff yourself across tiers and models. Switching is one flag:

| Flag | Tier | Behaviour |
|------|------|-----------|
| `--service-tier priority` | realtime | calls fire concurrently and block |
| `--service-tier flex --background` | async | swarm submitted as background jobs, then polled |

Run both over the identical workload:

```bash
dw project run compare -- --repo psf/requests --max-files 20
```

`compare` writes a wall-clock / token / cost table to `results/<slug>/analysis.md`.
Cost is `measured tokens ×` the per-(model, tier) rate table in `src/cost.py`, summed
across every model the run used (relevant when `--worker-model` routes workers to a
cheaper model than the orchestrator/synthesizer).

## Reading the parallelism (critical steps)

Every run reports two step counts in `summary.json` and the run footer, mirroring the
PARL paper's *critical-steps* metric (K2.5 tech report §3):

- **total** — every agent turn the run made (the sequential-equivalent work).
- **critical** — the longest dependent path: each orchestrator turn, plus `max(worker
  rounds)` per wave (a wave finishes when its slowest worker finishes), plus `max(verifier
  rounds)`, plus synthesis. This is what wall-clock tracks when fan-out is truly parallel.

`speedup = total / critical` is the swarm's own decomposition quality — how much the
orchestrator actually parallelized, scored the way the paper scores it. A speedup near
1.0 means the orchestrator serialized (one worker, or workers that each needed many tool
rounds); a higher number means it spread independent work across the cohort. This is an
*intrinsic* measure (it doesn't depend on tier), so it's comparable across `priority` and
`flex` runs and across the `structured` vs `kimi` interfaces.

## Measuring cost accurately

- **Whether async is cheaper depends on the model.** A `flex` (or batch) discount
  only applies if the model has discounted tier pricing configured — some models do,
  some don't. Because `--model` is a runtime parameter, point `compare` at the model
  you actually plan to use.
- **Treat `dw usage` as the source of truth for cost.** The in-tool figure is computed
  from the API's reported token usage and is a guide; confirm real spend with billing:

```bash
dw usage --since $(date +%Y-%m-%d)     # actual cost, by model
dw requests                             # per-request input/output_price_per_token
```

To seed real rates for a model, read its `input_price_per_token` /
`output_price_per_token` from `dw requests` and add an entry to `RATES` in
[`src/cost.py`](src/cost.py).
