"""Configuration management for Web Lobster.

Loads settings from YAML files with sensible defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """Configuration for a single model role."""
    backend: str = "ollama"          # "ollama", "llamacpp", or "anthropic"
    model: str = "qwen2.5:7b"       # Ollama model tag, GGUF path, or Claude model ID
    base_url: str = "http://localhost:11434"  # Ollama API URL (ignored for anthropic)
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout: float = 120.0
    grammar_path: Optional[str] = None  # GBNF grammar file for llama.cpp
    api_key: Optional[str] = None       # Anthropic API key (falls back to ANTHROPIC_API_KEY env var)


class BrowserConfig(BaseModel):
    headless: bool = False           # False = watch the browser work
    viewport_width: int = 1280
    viewport_height: int = 900
    default_timeout: float = 30.0    # seconds
    screenshot_quality: int = 80     # JPEG quality for screenshots
    slow_mo: float = 0.0            # ms delay between actions (for debugging)


class SafetyConfig(BaseModel):
    """Safety rails to prevent destructive autonomous actions."""
    dry_run: bool = False            # log actions without executing
    confirm_before: list[str] = Field(
        default_factory=lambda: ["submit", "purchase", "delete", "send", "pay"]
    )
    url_allowlist: list[str] = Field(default_factory=list)  # empty = allow all
    url_blocklist: list[str] = Field(
        default_factory=lambda: ["*/admin/*", "*/settings/delete*"]
    )
    max_actions_per_subgoal: int = 30
    ensemble_voting: bool = False     # require multi-model consensus
    ensemble_models: list[str] = Field(
        default_factory=lambda: ["phi3:mini", "llama3.2:3b"]
    )


class AgentConfig(BaseModel):
    max_steps: int = 100
    max_replans: int = 3
    validation_confidence_threshold: float = 0.7
    action_retry_limit: int = 2
    screenshot_mode: str = "hybrid"  # "hybrid", "screenshot_only", "dom_only"
    max_seconds: int = 600           # wall-clock timeout; 0 = no limit


class WebLobsterConfig(BaseModel):
    """Top-level configuration."""
    planner: ModelConfig = Field(default_factory=lambda: ModelConfig(
        model="qwen2.5:72b",
        temperature=0.2,
        max_tokens=4096,
    ))
    executor: ModelConfig = Field(default_factory=lambda: ModelConfig(
        model="qwen2.5:7b",
        temperature=0.0,
        max_tokens=512,
    ))
    validator: ModelConfig = Field(default_factory=lambda: ModelConfig(
        model="minicpm-v:8b",
        temperature=0.1,
        max_tokens=512,
    ))
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> WebLobsterConfig:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    @classmethod
    def default(cls) -> WebLobsterConfig:
        return cls()
