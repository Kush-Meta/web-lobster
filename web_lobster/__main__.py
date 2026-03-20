"""CLI entry point for Web Lobster.

Usage:
    python -m web_lobster run "Find the cheapest flight from LAX to JFK"
    python -m web_lobster ui
    python -m web_lobster ui --port 8080
    python -m web_lobster run --config my_config.yaml "Search for Python jobs"
    python -m web_lobster run --dry-run "Buy tickets to the concert"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.utils.logging import print_banner, set_log_level, get_logger

logger = get_logger("cli")


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """🦞 Web Lobster — Autonomous web agent powered by open-source AI"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument("task")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to YAML config file")
@click.option("--start-url", "-u", default="https://www.google.com", help="URL to start the browser at")
@click.option("--dry-run", is_flag=True, help="Log actions without executing them")
@click.option("--headless", is_flag=True, help="Run browser in headless mode")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--max-steps", type=int, default=None, help="Maximum agent steps")
def run(task, config, start_url, dry_run, headless, verbose, max_steps):
    """Run a task from the command line.

    Examples:

        python -m web_lobster run "Search Google for best pizza in NYC"

        python -m web_lobster run -u https://github.com "Star the playwright repo"
    """
    print_banner()
    if verbose:
        set_log_level("DEBUG")

    cfg = WebLobsterConfig.from_yaml(config) if config else WebLobsterConfig.default()
    if dry_run:
        cfg.safety.dry_run = True
    if headless:
        cfg.browser.headless = True
    if max_steps is not None:
        cfg.agent.max_steps = max_steps

    logger.info("config_loaded", dry_run=cfg.safety.dry_run, headless=cfg.browser.headless)
    logger.info("task", task=task)

    orchestrator = Orchestrator(cfg)
    result = asyncio.run(orchestrator.run(task, start_url=start_url))
    click.echo(result.summary())
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
    cfg = WebLobsterConfig.from_yaml(config) if config else WebLobsterConfig.default()

    from web_lobster.ui.server import run_server
    logger.info("launching_dashboard", url=f"http://{host}:{port}")
    click.echo(f"  Dashboard: http://{host}:{port}\n")
    run_server(host=host, port=port, config=cfg)


def main():
    cli()


if __name__ == "__main__":
    main()
