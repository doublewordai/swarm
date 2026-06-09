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


def _resolve_model(m: str) -> str:
    return MODEL_ALIASES.get(m, m)


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


def _write_results(output, slug, brief, res, cfg, wall) -> dict:
    d = Path(output) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(res["report"])
    (d / f"{res['result_key']}.json").write_text(json.dumps(res["results"], indent=2))
    (d / "swarm-tree.json").write_text(json.dumps(res["agents"], indent=2))
    tk = res["tokens"]
    c = cost.compute_cost(cfg.model, cfg.service_tier, tk["input_tokens"],
                          tk["output_tokens"] + tk.get("reasoning_tokens", 0))
    summary = {
        "brief": brief.name, "result_key": res["result_key"], "slug": slug, "model": cfg.model,
        "service_tier": cfg.service_tier, "background": cfg.background, "tokens": tk,
        "wall_clock_s": round(wall, 1), "cost_usd": c["cost_usd"], "rate_known": c["rate_known"],
        "coverage": res["coverage"], "waves": res["waves"], "n_results": len(res["results"]),
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _make_cfg(model_id, service_tier, background, max_files, max_agents, max_waves,
              max_rounds, no_verify, search_enabled):
    return engine.SwarmConfig(
        model=model_id, service_tier=service_tier, background=background,
        max_files=max_files, max_agents=max_agents, max_waves=max_waves,
        max_rounds=max_rounds, verify=not no_verify, search_enabled=search_enabled,
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
@click.option("-m", "--model", default="k2.6", help="Model alias (k2.6, k2.5) or full model_name")
@click.option("--service-tier", type=click.Choice(["priority", "flex"]), default="priority")
@click.option("--background/--no-background", default=None,
              help="Background submit-then-poll (defaults on for flex)")
@click.option("--max-files", default=40, help="Cap on source files")
@click.option("--max-agents", default=12, help="Cap on parallel workers")
@click.option("--max-waves", default=2, help="Max orchestrator dispatch waves")
@click.option("--max-rounds", default=3, help="Max tool rounds per worker")
@click.option("--no-verify", is_flag=True, help="Skip the verifier stage (if the brief has one)")
@click.option("--enable-search/--no-enable-search", default=None,
              help="Web-search grounding tools (default: on iff SERPER_API_KEY is set)")
@click.option("-o", "--output", default="results/", help="Output directory")
@click.option("--dry-run", is_flag=True, help="Build & print the plan; no API calls")
def run(brief, repo_, path_, model, service_tier, background, max_files, max_agents,
        max_waves, max_rounds, no_verify, enable_search, output, dry_run):
    """Run a BRIEF over a repo/path (see `swarm briefs`)."""
    if bool(repo_) == bool(path_):
        raise click.UsageError("provide exactly one of --repo or --path")
    b = _resolve_brief(brief)
    model_id = _resolve_model(model)
    if background is None:
        background = service_tier == "flex"
    search_enabled = enable_search if enable_search is not None else bool(os.environ.get("SERPER_API_KEY"))

    root, slug, files, dropped = _prepare(repo_, path_, max_files, output)
    out_slug = f"{b.name}-{slug}"
    cfg = _make_cfg(model_id, service_tier, background, max_files, max_agents, max_waves,
                    max_rounds, no_verify, search_enabled)
    click.echo(f"Brief:   {b.name} — {b.description}")
    click.echo(f"Model:   {model_id}")
    click.echo(f"Target:  {slug}  ({len(files)} source files)")
    click.echo(f"Tier:    {service_tier} (background={background})")
    if dropped:
        shown = ", ".join(dropped[:10]) + (" ..." if len(dropped) > 10 else "")
        click.echo(f"NOTE: capped at {max_files} files; skipped {len(dropped)}: {shown}")

    if dry_run:
        rtool = tools.submit_results_tool(b.result_key, b.result_schema, b.submit_description)
        click.echo("\n--- DRY RUN (no API calls) ---")
        click.echo("Orchestrator: " + ", ".join(t["name"] for t in tools.tools_for("orchestrator")))
        click.echo("Worker:       " + ", ".join(t["name"] for t in tools.tools_for(
            "worker", worker_tools=b.worker_tools, search_enabled=search_enabled, results_tool=rtool)))
        vtools = (", ".join(t["name"] for t in tools.tools_for(
            "verifier", verifier_tools=b.verifier_tools, search_enabled=search_enabled))
            if b.verifier_prompt else "(no verify stage for this brief)")
        click.echo("Verifier:     " + vtools)
        rmap = repo.build_repo_map(root, files)
        click.echo(f"\nRepo map ({len(rmap)} chars), first 2000:\n")
        click.echo(rmap[:2000])
        return

    client = R.make_client("doubleword")
    t0 = time.time()
    res = engine.run_swarm(client, b, root, files, cfg,
                           on_event=lambda kind, msg: click.echo(f"  [{kind}] {msg}"))
    wall = time.time() - t0
    summary = _write_results(output, out_slug, b, res, cfg, wall)

    d = Path(output) / out_slug
    click.echo()
    click.echo(f"{b.result_key.capitalize()}: {summary['n_results']}  ·  "
               f"coverage {res['coverage']['assigned']}/{res['coverage']['total']} files  ·  waves {res['waves']}")
    tk = res["tokens"]
    click.echo(f"Tokens:  {tk['input_tokens']:,} in / {tk['output_tokens']:,} out "
               f"(+{tk.get('reasoning_tokens', 0):,} reasoning)")
    if summary["rate_known"]:
        click.echo(f"Cost:    ${summary['cost_usd']:.4f} ({service_tier})   (use `dw usage` for actual spend)")
    else:
        click.echo(f"Cost:    rate not seeded for {model_id}/{service_tier} (see cost.RATES); use `dw usage`")
    click.echo(f"Wall:    {wall:.1f}s")
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
        res = engine.run_swarm(client, b, root, files, cfg,
                               on_event=lambda kind, msg: click.echo(f"  [{kind}] {msg}"))
        wall = time.time() - t0
        _write_results(output, f"{b.name}-{slug}-{tier}", b, res, cfg, wall)
        tk = res["tokens"]
        c = cost.compute_cost(model_id, tier, tk["input_tokens"],
                              tk["output_tokens"] + tk.get("reasoning_tokens", 0))
        rows.append({"tier": tier, "background": bg, "tokens": tk, "wall": wall,
                     "cost": c, "n": len(res["results"])})
        click.echo()

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
