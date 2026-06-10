"""CLI for the Doubleword Agent Swarm.

`swarm run <brief> ...` runs the generic engine with a chosen brief (audit, onboarding,
or your own). `report` prints the latest run, `compare` runs a brief in both tiers, and
`briefs` lists what's available. The target model is a runtime parameter (default Kimi K2.6).
"""

import json
import os
import time
from pathlib import Path

import click

from . import briefs as briefs_mod
from . import cost
from . import engine
from . import responses as R
from . import tools
from .tools import repo

MODEL_ALIASES = {"k2.6": "moonshotai/Kimi-K2.6", "k2.5": "moonshotai/Kimi-K2.5"}
DEFAULT_MODEL = "moonshotai/Kimi-K2.6"


def _resolve_model(m: str, provider: str = "doubleword") -> str:
    # Aliases name Doubleword model_names; other providers use their own ids, so
    # pass the model through verbatim there (e.g. gpt-5.2 on openai).
    return MODEL_ALIASES.get(m, m) if provider == "doubleword" else m


def _resolve_brief(name: str):
    try:
        return briefs_mod.get_brief(name)
    except KeyError as exc:
        raise click.ClickException(str(exc))


def _prepare(repo_, path_, max_files, output):
    root, slug = repo.resolve_source(repo_, path_, str(Path(output) / "_repos"))
    files = repo.list_source_files(root)
    dropped = []
    if len(files) > max_files:
        dropped, files = files[max_files:], files[:max_files]
    return root, slug, files, dropped


def _event_printer(verbosity: int):
    """Render the engine's event stream at a verbosity level.

    0: headline events only (the dispatch/wave/verify/synthesize milestones).
    1 (-v): + a per-call line (role, agent, elapsed, tokens, finish) and the
            orchestrator's dispatch plan, plus any failed call shown live.
    2 (-vv): + each agent's tool calls.
    """
    def render_call(data: dict) -> None:
        el = data.get("elapsed_s")
        el_s = f"{el:.1f}s" if isinstance(el, (int, float)) else "  ?  "
        line = (f"    · {data.get('role', '?'):<12} {data.get('agent', ''):<14} "
                f"{el_s:>7}  {data.get('tokens', 0):>8,} tok  {data.get('finish', '')}")
        if data.get("error"):
            line += f"  ERROR: {data['error']}"
        click.echo(line)
        if verbosity >= 2 and data.get("tool_calls"):
            click.echo(f"        tools: {', '.join(data['tool_calls'])}")

    def handler(kind, msg, data=None):
        if kind == "call":
            if verbosity >= 1 and data:
                render_call(data)
            return
        if kind == "plan":
            if verbosity >= 1 and data:
                team = ", ".join(f"{w['role']}({w['n_files']}f)" for w in data.get("workers", []))
                click.echo(f"  [plan] wave {data.get('wave', '?')}: {team}")
            return
        click.echo(f"  [{kind}] {msg}")

    return handler


def _cost_of(cfg, tk) -> dict:
    """Cost across all models used (orchestrator/synth on cfg.model, workers on worker_model)."""
    by_model = tk.get("by_model") or {cfg.model: tk}
    total, known = 0.0, True
    for model, mt in by_model.items():
        c = cost.compute_cost(model, cfg.service_tier, mt["input_tokens"],
                              mt["output_tokens"] + mt.get("reasoning_tokens", 0))
        if c["rate_known"]:
            total += c["cost_usd"]
        else:
            known = False
    return {"cost_usd": total if known else None, "rate_known": known}


def _write_results(output, slug, brief, res, cfg, wall) -> dict:
    d = Path(output) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(res["report"])
    (d / f"{res['result_key']}.json").write_text(json.dumps(res["results"], indent=2))
    (d / "swarm-tree.json").write_text(json.dumps(res["agents"], indent=2))
    tk = res["tokens"]
    c = _cost_of(cfg, tk)
    summary = {
        "brief": brief.name, "result_key": res["result_key"], "slug": slug, "model": cfg.model,
        "worker_model": cfg.worker_model, "interface": res.get("interface", cfg.interface),
        "service_tier": cfg.service_tier, "background": cfg.background, "tokens": tk,
        "wall_clock_s": round(wall, 1), "cost_usd": c["cost_usd"], "rate_known": c["rate_known"],
        "coverage": res["coverage"], "waves": res["waves"], "steps": res.get("steps"),
        "n_results": len(res["results"]), "n_refuted": res.get("n_refuted", 0),
        "errors": res.get("errors", []),
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _parse_temperature(raw):
    """CLI --temperature → cfg.temperature: None (role defaults) | float | "omit"."""
    if raw is None:
        return None
    if raw.strip().lower() in ("none", "omit"):
        return "omit"
    try:
        return float(raw)
    except ValueError:
        raise click.UsageError(f"--temperature must be a number or 'none', got {raw!r}")


# Solo mode gives the single agent a large context window and output budget so it can
# take in (most of) the repo and emit all findings in one turn.
SOLO_CONTEXT_CHARS = 3_000_000   # ~750k tokens preloaded; the rest via read_file/grep
SOLO_OUTPUT_TOKENS = 32768


def _make_cfg(model_id, worker_model_id, interface, service_tier, background, max_files,
              max_agents, max_waves, max_steps, max_rounds, max_files_per_worker,
              max_concurrent, verify_votes, no_verify, search_enabled,
              reasoning_effort, temperature, solo):
    big = {"worker_context_chars": SOLO_CONTEXT_CHARS,
           "worker_max_output_tokens": SOLO_OUTPUT_TOKENS} if solo else {}
    return engine.SwarmConfig(
        model=model_id, worker_model=worker_model_id, interface=interface, solo=solo,
        service_tier=service_tier, background=background, max_files=max_files,
        max_agents=max_agents, max_waves=max_waves, max_steps=max_steps,
        max_rounds=max_rounds, max_files_per_worker=max_files_per_worker,
        max_concurrent=max_concurrent, verify_votes=verify_votes,
        verify=not no_verify, search_enabled=search_enabled,
        reasoning_effort=(None if reasoning_effort == "none" else reasoning_effort),
        temperature=_parse_temperature(temperature), **big,
    )


@click.group()
def cli():
    """Doubleword Agent Swarm — a self-designing swarm you point at a task via a brief."""


@cli.command()
def briefs():
    """List available briefs."""
    for name in briefs_mod.list_briefs():
        b = briefs_mod.get_brief(name)
        click.echo(f"  {name:<12} {b.description}")


@cli.command()
@click.argument("brief")
@click.option("--repo", "repo_", default=None, help="GitHub repo 'owner/name' (shallow-cloned)")
@click.option("--path", "path_", default=None, help="Local directory to work over")
@click.option("--provider", type=click.Choice(sorted(R.PROVIDERS)), default="doubleword",
              help="API provider (also sets base URL + API-key env var)")
@click.option("-m", "--model", default="k2.6", help="Model alias (k2.6, k2.5) or full model_name")
@click.option("--worker-model", default=None,
              help="Model for workers/verifiers (cheap workers, strong orchestrator). Default: --model")
@click.option("--reasoning-effort", type=click.Choice(["minimal", "low", "medium", "high", "none"]),
              default="minimal", help="Reasoning depth; 'none' omits the param (non-reasoning models)")
@click.option("--temperature", default=None,
              help="Sampling temperature (number), or 'none' to omit it (gpt-5-class models reject it)")
@click.option("--interface", type=click.Choice(["structured", "kimi"]), default="structured",
              help="structured (dispatch_workers) or kimi (create_subagent/assign_task, K2.5 §E.8)")
@click.option("--solo", is_flag=True,
              help="Single-agent baseline: one agent audits the whole repo, no orchestration "
                   "(big context + output budget). Ignores --interface.")
@click.option("--service-tier", type=click.Choice(["priority", "flex"]), default="priority")
@click.option("--background/--no-background", default=None,
              help="Background submit-then-poll (defaults on for flex)")
@click.option("--max-files", default=500, help="Cap on source files")
@click.option("--max-agents", default=100, help="Cap on total workers across the run")
@click.option("--max-waves", default=2, help="Max dispatch_workers waves (structured interface)")
@click.option("--max-steps", default=8, help="Max orchestrator turns")
@click.option("--max-rounds", default=3, help="Max tool rounds per worker/verifier")
@click.option("--max-files-per-worker", default=30, help="Split worker specs larger than this")
@click.option("--max-concurrent", default=12, help="Max in-flight requests per dispatch")
@click.option("--timeout", default=R.DEFAULT_TIMEOUT, show_default=True,
              help="Per-request timeout (s); raise for very large orchestrator turns")
@click.option("--verify-votes", default=1, help="Verifiers per finding; majority decides")
@click.option("--no-verify", is_flag=True, help="Skip the verifier stage (if the brief has one)")
@click.option("--enable-search/--no-enable-search", default=None,
              help="Web-search grounding tools (default: on iff SERPER_API_KEY is set)")
@click.option("-o", "--output", default="results/", help="Output directory")
@click.option("-v", "--verbose", count=True,
              help="-v: per-call timing/tokens + dispatch plan + live failures. -vv: + tool calls")
@click.option("--dry-run", is_flag=True, help="Build & print the plan; no API calls")
def run(brief, repo_, path_, provider, model, worker_model, reasoning_effort, temperature,
        interface, solo, service_tier, background, max_files, max_agents, max_waves, max_steps,
        max_rounds, max_files_per_worker, max_concurrent, timeout, verify_votes, no_verify,
        enable_search, output, verbose, dry_run):
    """Run a BRIEF over a repo/path (see `swarm briefs`)."""
    if bool(repo_) == bool(path_):
        raise click.UsageError("provide exactly one of --repo or --path")
    if provider != "doubleword" and model == "k2.6":
        raise click.UsageError(
            f"--model is required for provider '{provider}' (the default 'k2.6' is a "
            f"Doubleword alias). Pass -m/--model with that provider's model id.")
    b = _resolve_brief(brief)
    model_id = _resolve_model(model, provider)
    worker_model_id = _resolve_model(worker_model, provider) if worker_model else None
    if background is None:
        background = service_tier == "flex"
    search_enabled = enable_search if enable_search is not None else bool(os.environ.get("SERPER_API_KEY"))

    root, slug, files, dropped = _prepare(repo_, path_, max_files, output)
    out_slug = f"{b.name}-{slug}"
    cfg = _make_cfg(model_id, worker_model_id, interface, service_tier, background, max_files,
                    max_agents, max_waves, max_steps, max_rounds, max_files_per_worker,
                    max_concurrent, verify_votes, no_verify, search_enabled,
                    reasoning_effort, temperature, solo)
    click.echo(f"Brief:   {b.name} — {b.description}")
    click.echo(f"Model:   {model_id}  (provider: {provider})"
               + (f"  (workers: {worker_model_id})" if worker_model_id else ""))
    click.echo(f"Target:  {slug}  ({len(files)} source files)")
    mode = "solo (single agent, no orchestration)" if solo else f"{interface} interface"
    click.echo(f"Mode:    {mode} · tier {service_tier} (background={background})")
    if dropped:
        shown = ", ".join(dropped[:10]) + (" ..." if len(dropped) > 10 else "")
        click.echo(f"NOTE: capped at {max_files} files; skipped {len(dropped)}: {shown}")

    if dry_run:
        rtool = tools.submit_results_tool(b.result_key, b.result_schema, b.submit_description)
        click.echo("\n--- DRY RUN (no API calls) ---")
        if solo:
            click.echo("Solo agent:   " + ", ".join(t["name"] for t in tools.tools_for(
                "worker", worker_tools=b.worker_tools, search_enabled=search_enabled, results_tool=rtool)))
            click.echo(f"Context:      up to {cfg.worker_context_chars:,} chars preloaded "
                       f"(rest via read_file/grep) · {cfg.worker_max_output_tokens:,} output tokens")
        else:
            click.echo("Orchestrator: " + ", ".join(
                t["name"] for t in tools.tools_for("orchestrator", interface=interface)))
            click.echo("Worker:       " + ", ".join(t["name"] for t in tools.tools_for(
                "worker", worker_tools=b.worker_tools, search_enabled=search_enabled, results_tool=rtool)))
        vtools = (", ".join(t["name"] for t in tools.tools_for(
            "verifier", verifier_tools=b.verifier_tools, search_enabled=search_enabled))
            + (f"  (×{verify_votes} votes)" if verify_votes > 1 else "")
            if b.verifier_prompt else "(no verify stage for this brief)")
        click.echo("Verifier:     " + vtools)
        rmap = repo.build_repo_map(root, files, max_chars=cfg.map_max_chars)
        click.echo(f"\nRepo map ({len(rmap)} chars), first 2000:\n")
        click.echo(rmap[:2000])
        return

    client = R.make_client(provider, timeout=timeout)
    t0 = time.time()
    try:
        res = engine.run_swarm(client, b, root, files, cfg, on_event=_event_printer(verbose))
    except engine.SwarmError as exc:
        raise click.ClickException(f"swarm failed: {exc}")
    wall = time.time() - t0
    summary = _write_results(output, out_slug, b, res, cfg, wall)

    d = Path(output) / out_slug
    click.echo()
    refuted = f"  ·  {res['n_refuted']} refuted" if res.get("n_refuted") else ""
    click.echo(f"{b.result_key.capitalize()}: {summary['n_results']}{refuted}  ·  "
               f"coverage {res['coverage']['assigned']}/{res['coverage']['total']} files  ·  waves {res['waves']}")
    tk = res["tokens"]
    click.echo(f"Tokens:  {tk['input_tokens']:,} in / {tk['output_tokens']:,} out "
               f"(+{tk.get('reasoning_tokens', 0):,} reasoning)")
    st = res.get("steps") or {}
    if st:
        click.echo(f"Steps:   {st['critical']} critical / {st['total']} total "
                   f"({st['speedup']}× parallel speedup)")
    if summary["rate_known"]:
        click.echo(f"Cost:    ${summary['cost_usd']:.4f} ({service_tier})   (use `dw usage` for actual spend)")
    else:
        click.echo(f"Cost:    rate not seeded for {model_id}/{service_tier} (see cost.RATES); use `dw usage`")
    click.echo(f"Wall:    {wall:.1f}s")
    if res.get("errors"):
        click.echo()
        for e in res["errors"]:
            click.echo(f"  WARNING: {e}")
    click.echo()
    click.echo(f"Results written to {d}/")
    click.echo(f"  report.md           the synthesized report (human-readable)")
    click.echo(f"  {res['result_key'] + '.json':<19} the structured results (machine-readable)")
    click.echo(f"  swarm-tree.json     agents the orchestrator spawned — roles, scopes, status")
    click.echo(f"  summary.json        brief, model, tier, tokens, cost, coverage")
    click.echo()
    click.echo("What to do with them:")
    click.echo(f"  • Read it:            dw project run report   (or open {d / 'report.md'})")
    click.echo(f"  • Structured output:  jq '.' {d / (res['result_key'] + '.json')}")
    click.echo("  • Re-run differently: another brief, --service-tier flex --background, or -m <model>")

    # The report itself failed to generate — results were preserved, but the run
    # did not deliver what it promised, so exit nonzero (loud, not silent).
    if any(e.startswith("synthesis failed") for e in res.get("errors", [])):
        raise click.ClickException("synthesis failed — results were written but the report was not generated")


@cli.command()
@click.option("-o", "--output", default="results/", help="Results directory")
def report(output):
    """Print the most recent run's report and summary."""
    base = Path(output)
    if not base.exists():
        raise click.ClickException(f"no results directory: {base}")
    runs = [d for d in base.iterdir() if d.is_dir() and (d / "summary.json").exists()]
    if not runs:
        raise click.ClickException("no completed runs found")
    runs.sort(key=lambda d: (d / "summary.json").stat().st_mtime, reverse=True)
    latest = runs[0]
    s = json.loads((latest / "summary.json").read_text())
    cost_str = (f"${s['cost_usd']:.4f}" if s.get("rate_known") else "rate n/a")
    click.echo("=" * 64)
    click.echo(f"{s.get('brief', '?')} · {s['slug']} · {s['model']} · {s['service_tier']}")
    click.echo(f"{s.get('result_key', 'results')}: {s['n_results']}  ·  tokens "
               f"{s['tokens']['input_tokens']:,}/{s['tokens']['output_tokens']:,}  ·  "
               f"cost {cost_str}  ·  {s['wall_clock_s']}s")
    click.echo("=" * 64)
    click.echo((latest / "report.md").read_text())
    click.echo(f"\nFull results in {latest}/")
    if len(runs) > 1:
        click.echo(f"({len(runs) - 1} older run(s) in {base})")


@cli.command()
@click.argument("brief")
@click.option("--repo", "repo_", default=None, help="GitHub repo 'owner/name'")
@click.option("--path", "path_", default=None, help="Local directory")
@click.option("-m", "--model", default="k2.6", help="Model alias or full model_name")
@click.option("--max-files", default=20, help="Cap on source files (smaller for a fair A/B)")
@click.option("--max-agents", default=8, help="Cap on parallel workers")
@click.option("--enable-search/--no-enable-search", default=None,
              help="Web-search grounding tools (default: on iff SERPER_API_KEY is set)")
@click.option("-o", "--output", default="results/", help="Output directory")
def compare(brief, repo_, path_, model, max_files, max_agents, enable_search, output):
    """Run a BRIEF over a repo in realtime (priority) and async (flex) tiers; write analysis.md."""
    if bool(repo_) == bool(path_):
        raise click.UsageError("provide exactly one of --repo or --path")
    b = _resolve_brief(brief)
    model_id = _resolve_model(model)
    search_enabled = enable_search if enable_search is not None else bool(os.environ.get("SERPER_API_KEY"))
    root, slug, files, dropped = _prepare(repo_, path_, max_files, output)
    client = R.make_client("doubleword")
    click.echo(f"Comparing tiers — brief {b.name} on {slug} ({len(files)} files), model {model_id}\n")

    rows = []
    for tier, bg in [("priority", False), ("flex", True)]:
        click.echo(f"=== {tier} (background={bg}) ===")
        cfg = engine.SwarmConfig(model=model_id, service_tier=tier, background=bg,
                                 max_files=max_files, max_agents=max_agents, verify=True,
                                 search_enabled=search_enabled)
        t0 = time.time()
        try:
            res = engine.run_swarm(client, b, root, files, cfg,
                                   on_event=lambda kind, msg: click.echo(f"  [{kind}] {msg}"))
        except engine.SwarmError as exc:
            click.echo(f"  swarm failed on {tier}: {exc}")
            click.echo()
            continue
        wall = time.time() - t0
        _write_results(output, f"{b.name}-{slug}-{tier}", b, res, cfg, wall)
        rows.append({"tier": tier, "background": bg, "tokens": res["tokens"], "wall": wall,
                     "cost": _cost_of(cfg, res["tokens"]), "n": len(res["results"])})
        click.echo()

    if not rows:
        raise click.ClickException("both tiers failed — nothing to compare")

    md = _comparison_md(b.name, slug, model_id, len(files), rows)
    out_path = Path(output) / f"{b.name}-{slug}"
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "analysis.md").write_text(md)
    click.echo(md)
    click.echo()
    click.echo(f"Comparison written to {out_path / 'analysis.md'}")
    click.echo("Cost figures use the rate table — confirm real spend with `dw usage`.")


def _comparison_md(brief_name, slug, model, n_files, rows) -> str:
    lines = [
        f"# Realtime vs Async — {brief_name} on {slug}",
        "",
        f"Model: `{model}` · {n_files} source files · same swarm, two service tiers.",
        "",
        "| Tier | background | Input tok | Output tok | Reasoning tok | Wall-clock | Cost | Results |",
        "|------|-----------|-----------|-----------|---------------|------------|------|---------|",
    ]
    for r in rows:
        cost_str = f"${r['cost']['cost_usd']:.4f}" if r["cost"]["rate_known"] else "rate n/a"
        lines.append(
            f"| {r['tier']} | {r['background']} | {r['tokens']['input_tokens']:,} | "
            f"{r['tokens']['output_tokens']:,} | {r['tokens'].get('reasoning_tokens', 0):,} | "
            f"{r['wall']:.1f}s | {cost_str} | {r['n']} |")
    return "\n".join(lines) + "\n"


def main():
    cli()
