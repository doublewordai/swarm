"""Cost computation from *measured* tokens and a per-(model, tier) rate table.

The harness only ever measures tokens (from the Responses ``usage`` field); cost
is measured-tokens x a documented rate. Rates are seeded from real ``dw usage``
observations (see the plan's live bring-up). Unseeded rates report as unknown
rather than a misleading $0.00.
"""

# $ per 1,000,000 tokens, as (input_rate, output_rate), per service tier.
# moonshotai/Kimi-K2.6 priority rate confirmed against `dw usage` billing. Seed other
# models/tiers from `dw requests` (input_price_per_token / output_price_per_token);
# a 0.0 rate is treated as "unknown" (reported as rate n/a), not free.
RATES: dict[str, dict[str, tuple[float, float]]] = {
    "moonshotai/Kimi-K2.6": {"priority": (0.95, 4.00), "flex": (0.0, 0.0)},
    "moonshotai/Kimi-K2.5": {"priority": (0.0, 0.0), "flex": (0.0, 0.0)},
}


def compute_cost(model: str, tier: str, input_tokens: int, output_tokens: int) -> dict:
    """Return {"cost_usd": float|None, "rate_known": bool}.

    rate_known is False when the (model, tier) pair is absent or still has a
    placeholder zero rate (unseeded).
    """
    entry = RATES.get(model, {}).get(tier)
    if not entry or (entry[0] == 0 and entry[1] == 0):
        return {"cost_usd": None, "rate_known": False}
    in_rate, out_rate = entry
    cost = input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate
    return {"cost_usd": cost, "rate_known": True}
