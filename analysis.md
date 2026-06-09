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
Cost is `measured tokens ×` the per-(model, tier) rate table in `src/cost.py`.

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
