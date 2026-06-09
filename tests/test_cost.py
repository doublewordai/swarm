from src import cost


def test_known_rate(monkeypatch):
    monkeypatch.setitem(cost.RATES, "m", {"priority": (1.0, 2.0)})
    r = cost.compute_cost("m", "priority", 1_000_000, 500_000)
    assert r["rate_known"]
    assert abs(r["cost_usd"] - 2.0) < 1e-9  # 1.0 (input) + 1.0 (0.5M * 2.0)


def test_unknown_rate():
    r = cost.compute_cost("nope", "flex", 10, 10)
    assert r["rate_known"] is False and r["cost_usd"] is None


def test_unseeded_zero_rate_is_unknown():
    r = cost.compute_cost("moonshotai/Kimi-K2.6", "flex", 100, 100)  # flex not yet seeded
    assert r["rate_known"] is False and r["cost_usd"] is None
