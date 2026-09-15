"""CLI entry point for Web Lobster.

Usage:
    python -m web_lobster run "Find the cheapest flight from LAX to JFK"
    python -m web_lobster run --mandate examples/mandate_flight_search.yaml
    python -m web_lobster bench
    python -m web_lobster mcp
    python -m web_lobster ui
    python -m web_lobster ui --port 8080
    python -m web_lobster run --config my_config.yaml "Search for Python jobs"
    python -m web_lobster run --dry-run "Buy tickets to the concert"
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import click

# Load .env file if present (keeps secrets out of shell profiles and committed code)
# Search from the package root upward so it works regardless of cwd.
try:
    from dotenv import load_dotenv
    _pkg_root = Path(__file__).resolve().parent.parent
    load_dotenv(_pkg_root / ".env")   # explicit path first
    load_dotenv()                     # also try cwd / shell env as fallback
except ImportError:
    pass

from web_lobster.bench.report import render as render_bench
from web_lobster.bench.runner import DEFENSES, defenses_by_name, run_benchmark
from web_lobster.bench.scenarios import SCENARIOS, scenario_by_id
from web_lobster.core.briefing import Question, TaskBrief, format_answer
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.values import ValueType
from web_lobster.mandate.schema import Mandate
from web_lobster.trials.report import render as render_trials, render_markdown as render_trials_markdown
from web_lobster.trials.runner import run_trials
from web_lobster.trials.spec import TrialFile
from web_lobster.verify.receipts import write_receipts
from web_lobster.utils.logging import print_banner, set_log_level, get_logger

logger = get_logger("cli")


def _load_config(path: str | None) -> WebLobsterConfig:
    """The config at path, used exactly as written. Without one, the default config,
    which plans and acts with Claude when ANTHROPIC_API_KEY is set.
    """
    if path:
        return WebLobsterConfig.from_yaml(path)
    return WebLobsterConfig.default()


def _parse_answers(pairs: tuple[str, ...]) -> dict[str, str]:
    answers = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"expected ID=VALUE, got {pair!r}", param_hint="--answer")
        answers[key.strip()] = value.strip()
    return answers


_YES_NO = {"y": "yes", "n": "no"}


async def _ask_in_terminal(questions: list[Question], brief: Optional[TaskBrief]) -> dict[str, object]:
    """Ask the planner's questions at the terminal. Enter accepts the default; a
    blank answer leaves the question to the planner's judgement."""
    click.echo("")
    if brief and brief.goal:
        click.echo(f"  🧠 {brief.goal}")
        for assumption in brief.assumptions:
            click.echo(f"     assuming {assumption}")
    click.echo("  Before I start:")
    answers: dict[str, object] = {}
    for question in questions:
        if question.why:
            click.echo(f"    ({question.why})")
        hint = "" if question.type == ValueType.TEXT else f" ({question.expected()})"
        default = "" if question.default is None else format_answer(question.default)
        while True:
            raw = click.prompt(f"  ? {question.question}{hint}", default=default, show_default=bool(default)).strip()
            if not raw:
                break
            value = question.coerce(_YES_NO.get(raw.lower(), raw))
            if value is not None:
                answers[question.id] = value
                break
            click.echo(f"    Please give {question.expected()}, or leave it blank.")
    click.echo("")
    return answers


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """🦞 Web Lobster — Autonomous web agent powered by open-source AI"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument("task", required=False)
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to YAML config file")
@click.option(
    "--mandate", "-m", type=click.Path(exists=True),
    help="Mandate YAML: the task plus the sites and data it may use, enforced by the browser",
)
@click.option(
    "--receipts", type=click.Path(dir_okay=False),
    help="Write the run's chained receipts to this JSONL file",
)
@click.option(
    "--start-url", "-u", default=None,
    help="URL to start the browser at (default: Google, or the mandate's first origin)",
)
@click.option("--dry-run", is_flag=True, help="Log actions without executing them")
@click.option("--headless", is_flag=True, help="Run browser in headless mode")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--max-steps", type=int, default=None, help="Maximum agent steps")
@click.option(
    "--notes", type=click.Path(exists=True, dir_okay=False),
    help="A file of standing notes for the planner, such as your city or preferences",
)
@click.option(
    "--answer", "answer_pairs", multiple=True, metavar="ID=VALUE",
    help="Answer one of the planner's questions ahead of time (repeatable)",
)
@click.option("--assume", is_flag=True, help="Don't ask questions: use defaults and best guesses")
@click.option("--no-brief", is_flag=True, help="Skip thinking the task through before starting")
def run(task, config, mandate, receipts, start_url, dry_run, headless, verbose, max_steps,
        notes, answer_pairs, assume, no_brief):
    """Run a task from the command line.

    Examples:

        python -m web_lobster run "Search Google for best pizza in NYC"

        python -m web_lobster run -u https://github.com "Star the playwright repo"

        python -m web_lobster run --mandate examples/mandate_flight_search.yaml

        python -m web_lobster run --mandate examples/mandate_flight_search.yaml --receipts run.jsonl

        python -m web_lobster run --notes ~/notes.md --answer dates=2026-12-15 "Find flights to Tokyo"

    Before the browser opens, the planner thinks the task through and may ask a
    few questions. At a terminal they're asked there; otherwise the run stops
    with exit code 2 and lists them, unless --assume is given.
    """
    print_banner()
    if verbose:
        set_log_level("DEBUG")

    task_mandate = Mandate.from_yaml(mandate) if mandate else None
    if task_mandate:
        if task and task != task_mandate.task:
            raise click.UsageError(
                "The task comes from the mandate; omit TASK or edit the mandate file."
            )
        task = task_mandate.task
        start_url = start_url or task_mandate.default_start_url()
        if not start_url:
            raise click.UsageError("The mandate only lists wildcard origins; pass --start-url.")
    elif not task:
        raise click.UsageError("Provide a TASK or a --mandate file.")
    start_url = start_url or "https://www.google.com"

    cfg = _load_config(config)
    if dry_run:
        cfg.safety.dry_run = True
    if headless:
        cfg.browser.headless = True
    if max_steps is not None:
        cfg.agent.max_steps = max_steps
    if no_brief:
        cfg.agent.briefing = False
    if assume:
        cfg.agent.questions = "assume"
    answers = _parse_answers(answer_pairs)
    notes_text = Path(notes).read_text() if notes else None
    # Questions are asked at the terminal only when someone is there to answer.
    asker = _ask_in_terminal if sys.stdin.isatty() and not assume else None

    logger.info("config_loaded", dry_run=cfg.safety.dry_run, headless=cfg.browser.headless)
    logger.info("task", task=task, mandate=bool(task_mandate))

    orchestrator = Orchestrator(cfg, mandate=task_mandate, asker=asker)
    result = asyncio.run(orchestrator.run(task, start_url=start_url, notes=notes_text, answers=answers))
    click.echo(result.summary())
    if receipts:
        write_receipts(result.receipts, receipts)
        click.echo(f"  Receipts written to {receipts}\n")
    if result.needs_input:
        sys.exit(2)
    sys.exit(0 if result.success else 1)


@cli.command()
@click.option("--host", "-h", default="127.0.0.1", help="Host to bind the dashboard server")
@click.option("--port", "-p", default=7860, type=int, help="Port for the dashboard server")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to YAML config file")
def ui(host, port, config):
    """Launch the interactive web dashboard.

    Opens a browser-based control panel where you can configure models,
    choose task presets, watch the agent work in real time, and
    approve/decline safety-flagged actions.

    Examples:

        python -m web_lobster ui

        python -m web_lobster ui --port 8080

        python -m web_lobster ui -c configs/lightweight.yaml
    """
    print_banner()
    cfg = _load_config(config)

    from web_lobster.ui.server import run_server
    logger.info("launching_dashboard", url=f"http://{host}:{port}")
    click.echo(f"  Dashboard: http://{host}:{port}\n")
    run_server(host=host, port=port, config=cfg)


@cli.command()
@click.option(
    "--mode", type=click.Choice(["scripted", "live"]), default="scripted", show_default=True,
    help="scripted: worst-case scripted models, no model calls. live: your configured models.",
)
@click.option(
    "--scenario", "scenario_ids", multiple=True, type=click.Choice([s.id for s in SCENARIOS]),
    help="Run only this scenario (repeatable)",
)
@click.option(
    "--defenses", "defense_names", multiple=True, type=click.Choice([d.name for d in DEFENSES]),
    help="Run only this defense configuration (repeatable)",
)
@click.option("--config", "-c", type=click.Path(exists=True), help="Model config for live mode")
@click.option("--json", "json_path", type=click.Path(dir_okay=False), help="Write every run's outcome as JSON")
@click.option("--headed", is_flag=True, help="Show the browser")
@click.option("--verbose", "-v", is_flag=True, help="Show agent logs")
def bench(mode, scenario_ids, defense_names, config, json_path, headed, verbose):
    """Run the poisoned-page benchmark against local trap sites.

    Each scenario is a small shop with a real task and a planted trap, plus a
    second site playing the attacker. Runs are scored from what those servers
    received, not from what the agent reports: did the task happen, did the
    trap's damage happen, did the email reach the attacker, and did the agent
    claim success that didn't happen.

    Scripted mode (the default) swaps the models for an executor that obeys
    every planted instruction and claims success early, plus an honest one for
    comparison. It shows what the defenses contain in the worst case, needs no
    API keys, and costs nothing. Live mode runs your configured models and
    makes real model calls.

    Examples:

        python -m web_lobster bench

        python -m web_lobster bench --scenario link-exfil --defenses none --defenses mandate+evidence

        python -m web_lobster bench --mode live -c configs/claude.yaml --json live.json
    """
    print_banner()
    set_log_level("DEBUG" if verbose else "SILENT")

    scenarios = [scenario_by_id(i) for i in scenario_ids] or list(SCENARIOS)
    defenses = [defenses_by_name(n) for n in defense_names] or list(DEFENSES)

    cfg = None
    if mode == "live":
        cfg = _load_config(config)
        click.echo(
            f"  Live mode: {len(scenarios) * len(defenses)} agent runs with your configured "
            "models. This makes real model calls.\n"
        )
    elif config:
        raise click.UsageError("--config only applies to --mode live.")

    def progress(outcome):
        click.echo(
            f"  {outcome.scenario:<20} {outcome.defenses:<17} {outcome.persona:<9} "
            f"{outcome.verdict()}  ({outcome.seconds:.1f}s)"
        )

    report = asyncio.run(run_benchmark(
        scenarios, defenses, mode=mode, config=cfg, headless=not headed, on_outcome=progress,
    ))
    click.echo(render_bench(report))
    if json_path:
        Path(json_path).write_text(report.model_dump_json(indent=2))
        click.echo(f"  Results written to {json_path}\n")


@cli.command()
@click.argument("trial_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--config", "-c", "config_paths", multiple=True, type=click.Path(exists=True, dir_okay=False),
    help="Model config to run (repeat to compare configs)",
)
@click.option("--runs", "-n", default=3, show_default=True, type=click.IntRange(1, 50), help="Runs per task and config")
@click.option("--task", "task_ids", multiple=True, help="Run only this task id (repeatable)")
@click.option(
    "--memory", type=click.Choice(["fresh", "shared"]), default="fresh", show_default=True,
    help="fresh: every run starts with empty memory. shared: runs of a task under one config share memory",
)
@click.option(
    "--runs-dir", type=click.Path(file_okay=False), default=None,
    help="Where run records go (default: a new folder under ~/.web_lobster/trials)",
)
@click.option("--json", "json_path", type=click.Path(dir_okay=False), help="Write every outcome as JSON")
@click.option("--markdown", "markdown_path", type=click.Path(dir_okay=False), help="Write the summary table as Markdown")
@click.option("--headed", is_flag=True, help="Show the browser")
@click.option("--verbose", "-v", is_flag=True, help="Show agent logs")
def trials(trial_file, config_paths, runs, task_ids, memory, runs_dir, json_path, markdown_path, headed, verbose):
    """Run live web tasks again and again against their known answers, and compare configs.

    A trial file lists tasks, each with a mandate, the values to read, and what
    they should be. Every task runs under every config the given number of
    times, and the report shows how often each was done, right, and verified,
    with median steps and time. Runs never stop to ask questions.

    This visits live sites and makes real model calls.

    Examples:

        web-lobster trials trials/web.yaml -c configs/local-16gb.yaml

        web-lobster trials trials/web.yaml --task everest -n 5 -c configs/local-16gb.yaml -c configs/local-16gb-14b-planner.yaml --markdown results.md
    """
    print_banner()
    set_log_level("DEBUG" if verbose else "SILENT")

    try:
        trial_file = TrialFile.from_yaml(trial_file)
    except Exception as e:
        raise click.UsageError(f"Can't read the trial file: {e}")
    known = {task.id for task in trial_file.tasks}
    unknown = sorted(set(task_ids) - known)
    if unknown:
        raise click.UsageError(f"Unknown task id(s): {', '.join(unknown)}. The file has: {', '.join(sorted(known))}")
    tasks = [task for task in trial_file.tasks if not task_ids or task.id in task_ids]

    configs: list[tuple[str, WebLobsterConfig]] = []
    for path in config_paths or [None]:
        cfg = _load_config(path)
        cfg.browser.headless = not headed
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(path).stem) if path else "default"
        while label in {existing for existing, _ in configs}:
            label += "_"
        configs.append((label, cfg))

    folder = Path(runs_dir) if runs_dir else Path.home() / ".web_lobster" / "trials" / time.strftime("%Y%m%d-%H%M%S")
    click.echo(
        f"  {len(tasks) * len(configs) * runs} live run(s): {len(tasks)} task(s) x {len(configs)} config(s) "
        f"x {runs} run(s). This visits live sites and makes real model calls.\n"
    )

    def progress(outcome):
        click.echo(
            f"  run {outcome.run}  {outcome.task:<16} {outcome.config:<30} {outcome.verdict()}  "
            f"({outcome.steps} steps, {outcome.seconds:.0f}s)"
        )

    report = asyncio.run(run_trials(tasks, configs, runs, runs_dir=folder, memory=memory, on_outcome=progress))
    click.echo(render_trials(report))
    click.echo(f"  Run records: {folder}\n")
    if json_path:
        Path(json_path).write_text(report.model_dump_json(indent=2))
        click.echo(f"  Outcomes written to {json_path}\n")
    if markdown_path:
        Path(markdown_path).write_text(render_trials_markdown(report))
        click.echo(f"  Summary written to {markdown_path}\n")


@cli.command("mcp")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to YAML config file")
@click.option(
    "--transport", type=click.Choice(["stdio", "streamable-http"]), default="stdio", show_default=True,
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for streamable HTTP")
@click.option("--port", default=8765, type=int, show_default=True, help="Port for streamable HTTP")
@click.option(
    "--runs-dir", type=click.Path(file_okay=False), default=None,
    help="Where run records and receipts are kept (default: ~/.web_lobster/runs)",
)
@click.option("--max-concurrent", default=1, type=int, show_default=True, help="Tasks run at once")
@click.option(
    "--approve-confirmations", is_flag=True,
    help="Approve the safety gate's confirmation prompts instead of declining them",
)
@click.option("--headed", is_flag=True, help="Show the browser")
@click.option("--verbose", "-v", is_flag=True, help="Debug logs (to stderr)")
def mcp_command(config, transport, host, port, runs_dir, max_concurrent, approve_confirmations, headed, verbose):
    """Serve web-lobster over MCP, so other agents can run web tasks under a mandate.

    Tools: web_task, check_mandate, get_run, verify_receipts. Speaks stdio by
    default; logs go to stderr because stdout carries the protocol. See
    docs/mcp-server.md for setup with OpenClaw and Claude Code.

    Examples:

        web-lobster mcp

        web-lobster mcp -c configs/claude.yaml --transport streamable-http --port 8765
    """
    from web_lobster.mcp_server.server import build_server
    from web_lobster.mcp_server.service import DEFAULT_RUNS_DIR, WebTaskService

    set_log_level("DEBUG" if verbose else "WARNING")
    if not verbose:
        # httpx logs every model call at INFO, which floods the host's server log.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = _load_config(config)
    cfg.browser.headless = not headed

    service = WebTaskService(
        cfg,
        runs_dir=Path(runs_dir) if runs_dir else DEFAULT_RUNS_DIR,
        max_concurrent=max_concurrent,
        approve_confirmations=approve_confirmations,
    )
    server = build_server(service)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=host, port=port)


def main():
    cli()


if __name__ == "__main__":
    main()
