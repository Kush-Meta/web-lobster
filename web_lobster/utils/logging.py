"""Structured logging for Web Lobster.

Uses Rich for pretty terminal output. Supports structured key-value
logging so every log line captures context (model name, action type,
element ID, etc.) without messy string formatting.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.layout import Layout
from rich.theme import Theme

# Custom theme for Web Lobster output
_theme = Theme({
    "lobster": "bold red",
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "bold red",
    "debug": "dim",
    "step": "bold magenta",
    "action": "bold blue",
    "model": "bold cyan",
})

_console = Console(theme=_theme, stderr=True)

# Global log level
_LOG_LEVEL = "INFO"
_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}


def set_log_level(level: str) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level.upper()


def _should_log(level: str) -> bool:
    return _LEVELS.get(level.upper(), 1) >= _LEVELS.get(_LOG_LEVEL, 1)


class Logger:
    """Structured logger for a specific component."""

    def __init__(self, name: str):
        self.name = name

    def _log(self, level: str, event: str, style: str, **kwargs: Any) -> None:
        if not _should_log(level):
            return

        # Format key-value pairs
        kv_parts = []
        for k, v in kwargs.items():
            if v is not None:
                kv_parts.append(f"[dim]{k}=[/dim]{v}")
        kv_str = " ".join(kv_parts)

        timestamp = time.strftime("%H:%M:%S")
        prefix = f"[dim]{timestamp}[/dim] [{style}]{level:>5}[/{style}]"
        component = f"[dim]{self.name}[/dim]"

        msg = f"{prefix} {component} {event}"
        if kv_str:
            msg += f" {kv_str}"

        _console.print(msg, highlight=False)

    def debug(self, event: str, **kwargs) -> None:
        self._log("DEBUG", event, "debug", **kwargs)

    def info(self, event: str, **kwargs) -> None:
        self._log("INFO", event, "info", **kwargs)

    def success(self, event: str, **kwargs) -> None:
        self._log("INFO", f"✓ {event}", "success", **kwargs)

    def warning(self, event: str, **kwargs) -> None:
        self._log("WARN", event, "warning", **kwargs)

    def error(self, event: str, **kwargs) -> None:
        self._log("ERROR", event, "error", **kwargs)

    def step(self, step_num: int, total: int, event: str, **kwargs) -> None:
        """Log an agent step with progress indicator."""
        self._log("INFO", f"[{step_num}/{total}] {event}", "step", **kwargs)

    def action(self, action_name: str, **kwargs) -> None:
        """Log a browser action."""
        self._log("INFO", f"→ {action_name}", "action", **kwargs)


def get_logger(name: str) -> Logger:
    return Logger(name)


class TaskDisplay:
    """Rich live terminal display showing real-time agent progress.

    Shows current task, sub-goal progress, recent actions, and stats
    in a compact panel that updates in real-time during execution.
    """

    def __init__(self):
        self._live: Optional[Live] = None
        self._stdout_console = Console()  # stdout for the live panel
        self._task = ""
        self._sub_goals: list[tuple[str, str]] = []  # (status, goal)
        self._recent_actions: list[str] = []
        self._step = 0
        self._max_steps = 100
        self._start_time = time.time()
        self._current_url = ""
        self._memory_hits = 0

    def start(self, task: str, max_steps: int = 100, memory_hits: int = 0) -> None:
        self._task = task
        self._max_steps = max_steps
        self._start_time = time.time()
        self._memory_hits = memory_hits
        self._live = Live(
            self._render(),
            console=self._stdout_console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

    def update_plan(self, sub_goals: list[tuple[str, str]]) -> None:
        self._sub_goals = sub_goals
        self._refresh()

    def update_action(self, step: int, action_str: str, url: str = "") -> None:
        self._step = step
        self._current_url = url
        self._recent_actions.append(action_str)
        if len(self._recent_actions) > 6:
            self._recent_actions = self._recent_actions[-6:]
        self._refresh()

    def update_subgoal_status(self, goal_id: int, status: str) -> None:
        if 0 <= goal_id < len(self._sub_goals):
            _, goal = self._sub_goals[goal_id]
            self._sub_goals[goal_id] = (status, goal)
        self._refresh()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    def _render(self):
        elapsed = time.time() - self._start_time
        status_icons = {"completed": "✓", "failed": "✗", "active": "▶", "pending": "○", "skipped": "⊘"}
        status_styles = {"completed": "green", "failed": "red", "active": "bold cyan", "pending": "dim", "skipped": "yellow"}

        # Sub-goals table
        goal_lines = Text()
        for status, goal in self._sub_goals:
            icon = status_icons.get(status, "○")
            style = status_styles.get(status, "")
            goal_lines.append(f"  {icon} {goal[:70]}\n", style=style)

        # Recent actions
        action_lines = Text()
        for a in self._recent_actions:
            action_lines.append(f"  → {a}\n", style="blue")

        # Stats line
        mem_note = f"  [dim]({self._memory_hits} memory hits)[/dim]" if self._memory_hits else ""
        stats = (
            f"[dim]Step {self._step}/{self._max_steps}  •  "
            f"{elapsed:.0f}s elapsed  •  {self._current_url[:60]}[/dim]{mem_note}"
        )

        layout = Layout()
        layout.split_column(
            Layout(Panel(
                Text(self._task[:120], style="bold"),
                title="[lobster]🦞 Web Lobster[/lobster]",
                subtitle=stats,
                border_style="red",
            ), size=4),
            Layout(Panel(
                goal_lines if goal_lines._spans else Text("Planning...", style="dim"),
                title="Sub-goals",
                border_style="cyan",
            ), size=min(len(self._sub_goals) + 3, 12)),
            Layout(Panel(
                action_lines if action_lines._spans else Text("Waiting for first action...", style="dim"),
                title="Recent Actions",
                border_style="blue",
            )),
        )
        return layout


def print_banner() -> None:
    """Print the Web Lobster startup banner."""
    _console.print(
        "\n[lobster]🦞 Web Lobster[/lobster] [dim]v0.1.0[/dim]",
        highlight=False,
    )
    _console.print(
        "[dim]Autonomous web agent powered by open-source AI[/dim]\n",
        highlight=False,
    )
