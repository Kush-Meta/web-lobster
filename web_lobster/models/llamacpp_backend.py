"""llama.cpp backend — uses the llama-cpp-python bindings.

The key advantage over Ollama: GBNF grammar constraints that force
the model to output valid JSON matching our action schema. This
eliminates parsing errors from the executor entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


# GBNF grammar that constrains output to valid Action JSON
ACTION_GRAMMAR = r"""
root        ::= "{" ws action ws "}"
ws          ::= [ \t\n]*
action      ::= click | type | scroll | navigate | wait | done | select | hover | go-back
click       ::= "\"action\":" ws "\"click\"," ws "\"element_id\":" ws number
type        ::= "\"action\":" ws "\"type\"," ws "\"element_id\":" ws number "," ws "\"text\":" ws string
scroll      ::= "\"action\":" ws "\"scroll\"," ws "\"direction\":" ws ("\"up\"" | "\"down\"")
navigate    ::= "\"action\":" ws "\"navigate\"," ws "\"url\":" ws string
wait        ::= "\"action\":" ws "\"wait\"," ws "\"seconds\":" ws number
done        ::= "\"action\":" ws "\"done\"," ws "\"reason\":" ws string
select      ::= "\"action\":" ws "\"select\"," ws "\"element_id\":" ws number "," ws "\"text\":" ws string
hover       ::= "\"action\":" ws "\"hover\"," ws "\"element_id\":" ws number
go-back     ::= "\"action\":" ws "\"go_back\""
number      ::= [0-9]+
string      ::= "\"" [^"\\]* "\""
"""


class LlamaCppBackend(ModelBackend):
    """Backend using llama-cpp-python for local inference with grammar constraints.

    Requires: pip install llama-cpp-python
    And a GGUF model file downloaded locally.
    """

    def __init__(
        self,
        model_path: str,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,  # -1 = offload all to GPU
        timeout: float = 120.0,
    ):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.timeout = timeout
        self._llm = None

    def _get_llm(self):
        """Lazy-load the model."""
        if self._llm is None:
            try:
                from llama_cpp import Llama
            except ImportError:
                raise ImportError(
                    "llama-cpp-python is required for the llama.cpp backend. "
                    "Install it with: pip install llama-cpp-python"
                )
            logger.info("loading_model", path=self.model_path)
            self._llm = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_gpu_layers=self.n_gpu_layers,
                verbose=False,
            )
        return self._llm

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        grammar: Optional[str] = None,
    ) -> str:
        # Note: llama-cpp-python is synchronous — in production you'd
        # want to run this in an executor thread pool
        llm = self._get_llm()

        full_prompt = ""
        if system:
            full_prompt += f"<|system|>\n{system}\n"
        full_prompt += f"<|user|>\n{prompt}\n<|assistant|>\n"

        kwargs = {
            "prompt": full_prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop": ["<|end|>", "<|user|>"],
        }

        # Apply GBNF grammar if provided
        if grammar:
            try:
                from llama_cpp import LlamaGrammar
                kwargs["grammar"] = LlamaGrammar.from_string(grammar)
            except ImportError:
                logger.warning("grammar_unavailable", reason="llama_cpp not installed")

        logger.debug("llamacpp_request", prompt_len=len(prompt), has_grammar=bool(grammar))

        output = llm(**kwargs)
        text = output["choices"][0]["text"].strip()

        logger.debug("llamacpp_response", response_len=len(text))
        return text

    async def is_available(self) -> bool:
        return Path(self.model_path).exists()

    @staticmethod
    def get_action_grammar() -> str:
        """Return the GBNF grammar for constraining executor output."""
        return ACTION_GRAMMAR
