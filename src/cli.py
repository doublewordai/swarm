"""CLI for the Doubleword agent-swarm code auditor.

Commands: ``audit`` (run the swarm), ``report`` (print latest results),
``compare`` (run the same repo in realtime + async tiers and write analysis.md).
The target model is a runtime parameter (``--model``), defaulting to Kimi K2.6.
"""

import json
import os
import time
from pathlib import Path

import click

from . import cost
from . import responses as R
from . import swarm
from . import tools
from .tools import repo

MODEL_ALIASES = {"k2.6": "moonshotai/Kimi-K2.6", "k2.5": "moonshotai/Kimi-K2.5"}
DEFAULT_MODEL = "moonshotai/Kimi-K2.6"


def _resolve_model(m: str) -> str:
    return MODEL_ALIASES.get(m, m)


def _prepare(repo_, path_, max_files, output):
    workdir = str(Path(output) / "_repos")
    root, slug = repo.resolve_source(repo_, path_, workdir)
    files = repo.list_source_files(root)
    dropped = []
    if len(files) > max_files:
        dropped = files[max_files:]
        files = files[:max_files]
    return root, slug, files, dropped


def _write_results(output, slug, res, cfg, wall) -> dict:
    d = Path(output) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.md").write_text(res["report"])
    (d / "findings.json").write_text(json.dumps(res["findings"], indent=2))
    (d / "swarm-tree.json").write_text(json.dumps(res["agents"], indent=2))
    tk = res["tokens"]
    out_billable = tk["output_tokens"] + tk.get("reasoning_tokens", 0)
    c = cost.compute_cost(cfg.model, cfg.service_tier, tk["input_tokens"], out_billable)
    summary = {
        "slug": slug, "model": cfg.model, "service_tier": cfg.service_tier,
        "background": cfg.background, "tokens": res["tokens"],
        "wall_clock_s": round(wall, 1), "cost_usd": c["cost_usd"],
        "rate_known": c["rate_known"], "coverage": res["coverage"],
        "waves": res["waves"], "n_findings": len(res["findings"]),
        "max_agents": cfg.max_agents, "max_files": cfg.max_files,
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _severity_counts(findings):
    counts = {}
    for f in findings:
        counts[f.get("severity", "info")] = counts.get(f.get("severity", "info"), 0) + 1
    return counts


@click.group()
def cli():
    """Doubleword agent-swarm code auditor over the Open Responses API."""


@cli.command()
@click.option("--repo", "repo_", default=None, help="GitHub repo 'owner/name' (shallow-cloned)")
@click.option("--path", "path_", default=None, help="Local directory to audit")
@click.option("-m", "--model", default="k2.6", help="Model alias (k2.6, k2.5) or full model_name")
@click.option("--service-tier", type=click.Choice(["priority", "flex"]), default="priority",
              help="priority = realtime; flex = async")
@click.option("--background/--no-background", default=None,
              help="Background submit-then-poll (defaults on for flex)")
@click.option("--max-files", default=40, help="Cap on source files audited")
@click.option("--max-agents", default=12, help="Cap on parallel workers")
@click.option("--max-waves", default=2, help="Max orchestrator dispatch waves")
@click.option("--max-rounds", default=3, help="Max tool rounds per worker")
@click.option("--no-verify", is_flag=True, help="Skip the adversarial verifier stage")
@click.option("--enable-search/--no-enable-search", default=None,
              help="Web-search grounding tools (default: on iff SERPER_API_KEY is set)")
@click.option("-o", "--output", default="results/", help="Output directory")
@click.option("--dry-run", is_flag=True, help="Build & print the plan; no API calls")
def audit(repo_, path_, model, service_tier, background, max_files, max_agents,
          max_waves, max_rounds, no_verify, enable_search, output, dry_run):
    """Run the Doubleword agent swarm to audit a codebase."""
    if bool(repo_) == bool(path_):
        raise click.UsageError("provide exactly one of --repo or --path")
    model_id = _resolve_model(model)
    if background is None:
        background = service_tier == "flex"
    search_enabled = enable_search if enable_search is not None else bool(os.environ.get("SERPER_API_KEY"))

    root, slug, files, dropped = _prepare(repo_, path_, max_files, output)
    click.echo(f"Model:   {model_id}")
    click.echo(f"Target:  {slug}  ({len(files)} source files)")
    click.echo(f"Tier:    {service_tier} (background={background})")
    click.echo("Tools:   read-only · run_sast · check_advisory"
               + (" · web_search/read_page" if search_enabled else "  (web search off)"))
    if dropped:
        shown = ", ".join(dropped[:10]) + (" ..." if len(dropped) > 10 else "")
        click.echo(f"NOTE: capped at {max_files} files; skipped {len(dropped)}: {shown}")

    cfg = swarm.SwarmConfig(
        model=model_id, service_tier=service_tier, background=background,
        max_files=max_files, max_agents=max_agents, max_waves=max_waves,
        max_rounds=max_rounds, verify=not no_verify, search_enabled=search_enabled,
    )

    if dry_run:
        click.echo("\n--- DRY RUN (no API calls) ---")
        click.echo("Orchestrator tools: " + ", ".join(t["name"] for t in tools.tools_for("orchestrator")))
        click.echo("Worker tools:       " + ", ".join(t["name"] for t in tools.tools_for("worker", search_enabled)))
        click.echo("Verifier tools:     " + ", ".join(t["name"] for t in tools.tools_for("verifier", search_enabled)))
        rmap = repo.build_repo_map(root, files)
        click.echo(f"\nRepo map ({len(rmap)} chars), first 2000:\n")
        click.echo(rmap[:2000])
        return

    client = R.make_client("doubleword")
    t0 = time.time()
    res = swarm.run_audit(client, root, files, cfg,
                          on_event=lambda kind, msg: click.echo(f"  [{kind}] {msg}"))
    wall = time.time() - t0
    summary = _write_results(output, slug, res, cfg, wall)

    click.echo()
    click.echo(f"Findings: {summary['n_findings']}  {_severity_counts(res['findings'])}")
    click.echo(f"Coverage: {res['coverage']['assigned']}/{res['coverage']['total']} files  "
               f"· waves: {res['waves']}")
    tk = res["tokens"]
    click.echo(f"Tokens:   {tk['input_tokens']:,} in / {tk['output_tokens']:,} out "
               f"(+{tk.get('reasoning_tokens', 0):,} reasoning)")
    click.echo("          (cost is a guide; use `dw usage` for actual spend)")
    if summary["rate_known"]:
        click.echo(f"Cost:     ${summary['cost_usd']:.4f} ({service_tier})")
    else:
        click.echo(f"Cost:     rate not seeded for {model_id}/{service_tier} (see cost.RATES)")
    click.echo(f"Wall:     {wall:.1f}s")

    d = Path(output) / slug
    click.echo()
    click.echo(f"Results written to {d}/")
    click.echo("  report.md        triaged audit (human-readable)")
    click.echo("  findings.json    findings + verifier verdicts (machine-readable)")
    click.echo("  swarm-tree.json  agents the orchestrator spawned — roles, scopes, status")
    click.echo("  summary.json     model, tier, tokens, cost, coverage")
    click.echo()
    click.echo("What to do with them:")
    click.echo(f"  • Read the audit:     dw project run report   (or open {d / 'report.md'})")
    click.echo(f"  • Findings for tools: jq '.[].title' {d / 'findings.json'}")
    click.echo("  • Re-run differently: add --service-tier flex --background, or -m <model>")


@cli.command()
@click.option("-o", "--output", default="results/", help="Results directory")
def report(output):
    """Print the most recent audit report and summary."""
    base = Path(output)
    if not base.exists():
        raise click.ClickException(f"no results directory: {base}")
    runs = [d for d in base.iterdir() if d.is_dir() and (d / "summary.json").exists()]
    if not runs:
        raise click.ClickException("no completed audits found")
    runs.sort(key=lambda d: (d / "summary.json").stat().st_mtime, reverse=True)
    latest = runs[0]
    summary = json.loads((latest / "summary.json").read_text())
    click.echo("=" * 64)
    click.echo(f"{summary['slug']}  ·  {summary['model']}  ·  {summary['service_tier']}")
    cost_str = (f"${summary['cost_usd']:.4f}" if summary.get("rate_known") else "rate n/a")
    click.echo(f"findings: {summary['n_findings']}  ·  tokens: "
               f"{summary['tokens']['input_tokens']:,}/{summary['tokens']['output_tokens']:,}  ·  "
               f"cost: {cost_str}  ·  {summary['wall_clock_s']}s")
    click.echo("=" * 64)
    click.echo((latest / "report.md").read_text())
    click.echo(f"\nFull results in {latest}/  —  report.md · findings.json · swarm-tree.json · summary.json")
    if len(runs) > 1:
        click.echo(f"({len(runs) - 1} older run(s) in {base})")


@cli.command()
@click.option("--repo", "repo_", default=None, help="GitHub repo 'owner/name'")
@click.option("--path", "path_", default=None, help="Local directory to audit")
@click.option("-m", "--model", default="k2.6", help="Model alias or full model_name")
@click.option("--max-files", default=20, help="Cap on source files (smaller for a fair A/B)")
@click.option("--max-agents", default=8, help="Cap on parallel workers")
@click.option("--enable-search/--no-enable-search", default=None,
              help="Web-search grounding tools (default: on iff SERPER_API_KEY is set)")
@click.option("-o", "--output", default="results/", help="Output directory")
def compare(repo_, path_, model, max_files, max_agents, enable_search, output):
    """Audit one repo in realtime (priority) and async (flex) tiers; write analysis.md."""
    if bool(repo_) == bool(path_):
        raise click.UsageError("provide exactly one of --repo or --path")
    model_id = _resolve_model(model)
    search_enabled = enable_search if enable_search is not None else bool(os.environ.get("SERPER_API_KEY"))
    root, slug, files, dropped = _prepare(repo_, path_, max_files, output)
    client = R.make_client("doubleword")
    click.echo(f"Comparing tiers on {slug} ({len(files)} files), model {model_id}\n")

    rows = []
    for tier, bg in [("priority", False), ("flex", True)]:
        click.echo(f"=== {tier} (background={bg}) ===")
        cfg = swarm.SwarmConfig(model=model_id, service_tier=tier, background=bg,
                                max_files=max_files, max_agents=max_agents, verify=True,
                                search_enabled=search_enabled)
        t0 = time.time()
        res = swarm.run_audit(client, root, files, cfg,
                              on_event=lambda kind, msg: click.echo(f"  [{kind}] {msg}"))
        wall = time.time() - t0
        _write_results(output, f"{slug}-{tier}", res, cfg, wall)
        out_billable = res["tokens"]["output_tokens"] + res["tokens"].get("reasoning_tokens", 0)
        c = cost.compute_cost(model_id, tier, res["tokens"]["input_tokens"], out_billable)
        rows.append({"tier": tier, "background": bg, "tokens": res["tokens"],
                     "wall": wall, "cost": c, "findings": len(res["findings"])})
        click.echo()

    md = _comparison_md(slug, model_id, len(files), rows)
    out_path = Path(output) / slug
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "analysis.md").write_text(md)
    click.echo(md)
    click.echo()
    click.echo(f"Comparison written to {out_path / 'analysis.md'}")
    click.echo(f"Per-tier results in {Path(output) / (slug + '-priority')}/ and "
               f"{Path(output) / (slug + '-flex')}/")
    click.echo("  (each has its own report.md · findings.json · swarm-tree.json · summary.json)")
    click.echo("Cost figures use the rate table — confirm real spend with `dw usage`.")


def _comparison_md(slug, model, n_files, rows) -> str:
    lines = [
        f"# Realtime vs Async — {slug}",
        "",
        f"Model: `{model}` · {n_files} source files · same swarm, two service tiers.",
        "",
        "| Tier | background | Input tok | Output tok | Reasoning tok | Wall-clock | Cost | Findings |",
        "|------|-----------|-----------|-----------|---------------|------------|------|----------|",
    ]
    for r in rows:
        cost_str = f"${r['cost']['cost_usd']:.4f}" if r["cost"]["rate_known"] else "rate n/a"
        lines.append(
            f"| {r['tier']} | {r['background']} | {r['tokens']['input_tokens']:,} | "
            f"{r['tokens']['output_tokens']:,} | {r['tokens'].get('reasoning_tokens', 0):,} | "
            f"{r['wall']:.1f}s | {cost_str} | {r['findings']} |"
        )
    if len(rows) == 2 and all(r["cost"]["rate_known"] for r in rows):
        p, fl = rows[0]["cost"]["cost_usd"], rows[1]["cost"]["cost_usd"]
        if p:
            lines += ["", f"Async (flex) cost is **{(1 - fl / p) * 100:.0f}% lower** than realtime "
                          f"for this run (wall-clock {rows[1]['wall']:.0f}s vs {rows[0]['wall']:.0f}s)."]
    return "\n".join(lines) + "\n"


def main():
    cli()
