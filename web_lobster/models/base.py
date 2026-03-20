"""Abstract interface for LLM backends.

Both Ollama and llama.cpp backends implement this interface,
so the planner/executor/validator don't care which backend is in use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ModelBackend(ABC):
    """Base class for all model backends."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,  # base64 encoded
        temperature: float = 0.1,
        max_tokens: int = 2048,
        grammar: Optional[str] = None,  # GBNF grammar string
    ) -> str:
        """Generate a text completion.

        Args:
            prompt: The user/input prompt
            system: Optional system prompt
            images: Optional list of base64-encoded images (for vision models)
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            grammar: Optional GBNF grammar to constrain output (llama.cpp only)

        Returns:
            The model's text response
        """
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Check if the backend and model are reachable."""
        ...
