"""Structured logging for Web Lobster.

Uses Rich for pretty terminal output. Supports structured key-value
logging so every log line captures context (model name, action type,
element ID, etc.) without messy string formatting.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from rich.console import Console
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
