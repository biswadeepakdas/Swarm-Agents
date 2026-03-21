"""
Multi-Model Router — routes each task type to the best-suited LLM.

Like Perplexity Computer's 19-model orchestration but practical:
uses litellm to support any provider, with static routing + fallbacks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from swarm.config import config
from swarm.models.task import TaskType

logger = logging.getLogger("swarm.model_router")


@dataclass
class ModelConfig:
    """Configuration for a specific model."""
    model: str           # litellm model string (e.g., "claude-sonnet-4-20250514", "gpt-4o")
    api_key_env: str     # Env var name for the API key
    api_base: str | None = None  # Custom API base URL (for NVIDIA NIM, etc.)
    max_tokens: int = 8000
    temperature: float = 0.7


# ── Model definitions ──────────────────────────────────────────────────
# Add your API keys as env vars to enable providers.
# Only models with valid API keys will be used.

MODELS: dict[str, ModelConfig] = {
    # Anthropic
    "claude-sonnet": ModelConfig(
        model="claude-sonnet-4-20250514",
        api_key_env="ANTHROPIC_API_KEY",
        max_tokens=8000,
        temperature=0.6,
    ),
    "claude-haiku": ModelConfig(
        model="claude-3-5-haiku-20241022",
        api_key_env="ANTHROPIC_API_KEY",
        max_tokens=4000,
        temperature=0.5,
    ),

    # OpenAI
    "gpt-4o": ModelConfig(
        model="gpt-4o",
        api_key_env="OPENAI_API_KEY",
        max_tokens=8000,
        temperature=0.7,
    ),
    "gpt-4o-mini": ModelConfig(
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        max_tokens=4000,
        temperature=0.5,
    ),

    # DeepSeek
    "deepseek-coder": ModelConfig(
        model="deepseek/deepseek-coder",
        api_key_env="DEEPSEEK_API_KEY",
        max_tokens=8000,
        temperature=0.3,
    ),

    # NVIDIA NIM (OpenAI-compatible)
    "nvidia-llama": ModelConfig(
        model="meta/llama-3.3-70b-instruct",
        api_key_env="NVIDIA_API_KEY",
        api_base="https://integrate.api.nvidia.com/v1",
        max_tokens=8000,
        temperature=0.7,
    ),

    # Google
    "gemini-pro": ModelConfig(
        model="gemini/gemini-1.5-pro",
        api_key_env="GEMINI_API_KEY",
        max_tokens=8000,
        temperature=0.7,
    ),
}


# ── Task → Model routing table ─────────────────────────────────────────
# First available model in the list wins (ordered by preference).
# If no configured model is available, falls back to config.llm_model.

TASK_ROUTING: dict[TaskType, list[str]] = {
    # Architecture & planning — needs strong reasoning
    TaskType.ANALYZE_REQUIREMENTS: ["claude-sonnet", "gpt-4o", "nvidia-llama"],
    TaskType.PLAN_ARCHITECTURE: ["claude-sonnet", "gpt-4o", "nvidia-llama"],
    TaskType.RESEARCH: ["gpt-4o", "gemini-pro", "claude-sonnet", "nvidia-llama"],

    # Code generation — needs strong code output
    TaskType.WRITE_CODE: ["claude-sonnet", "deepseek-coder", "gpt-4o", "nvidia-llama"],
    TaskType.CREATE_API: ["claude-sonnet", "deepseek-coder", "gpt-4o", "nvidia-llama"],
    TaskType.FIX_CODE: ["claude-sonnet", "deepseek-coder", "gpt-4o", "nvidia-llama"],
    TaskType.BUILD_FRONTEND_COMPONENT: ["claude-sonnet", "gpt-4o", "nvidia-llama"],

    # Design — creative tasks
    TaskType.DESIGN_UI: ["claude-sonnet", "gpt-4o", "nvidia-llama"],
    TaskType.DESIGN_DATABASE: ["claude-sonnet", "gpt-4o", "deepseek-coder", "nvidia-llama"],

    # Review — analytical tasks
    TaskType.REVIEW_CODE: ["claude-sonnet", "gpt-4o", "nvidia-llama"],
    TaskType.DEBUG: ["claude-sonnet", "deepseek-coder", "gpt-4o", "nvidia-llama"],

    # Testing
    TaskType.WRITE_TESTS: ["claude-sonnet", "deepseek-coder", "gpt-4o", "nvidia-llama"],
    TaskType.INTEGRATION_TEST: ["claude-sonnet", "gpt-4o", "nvidia-llama"],

    # Docs & ops — lighter tasks
    TaskType.WRITE_DOCS: ["gpt-4o-mini", "claude-haiku", "nvidia-llama"],
    TaskType.DEPLOY: ["claude-haiku", "gpt-4o-mini", "nvidia-llama"],
    TaskType.RESOLVE_CONFLICT: ["claude-sonnet", "gpt-4o", "nvidia-llama"],
}


class ModelRouter:
    """Routes task types to the best available LLM model."""

    def __init__(self) -> None:
        self._available_models: dict[str, ModelConfig] = {}
        self._detect_available_models()

    def _detect_available_models(self) -> None:
        """Check which models have valid API keys configured."""
        # Also check the default config key
        default_key = config.llm_api_key

        for name, model_cfg in MODELS.items():
            key = os.getenv(model_cfg.api_key_env, "")

            # Special: NVIDIA NIM can use the default LLM_API_KEY
            if not key and "nvidia" in name.lower() and "nvidia" in config.llm_provider.lower():
                key = default_key

            if key:
                self._available_models[name] = model_cfg

        available_names = list(self._available_models.keys())
        logger.info(f"ModelRouter: {len(available_names)} models available: {available_names}")

    def select_model(self, task_type: TaskType) -> ModelConfig:
        """Select the best available model for a task type."""
        preferences = TASK_ROUTING.get(task_type, [])

        # Try each preferred model in order
        for model_name in preferences:
            if model_name in self._available_models:
                cfg = self._available_models[model_name]
                logger.debug(f"Routing {task_type.value} → {model_name} ({cfg.model})")
                return cfg

        # Fallback: use the default config model
        logger.debug(f"Routing {task_type.value} → default ({config.llm_model})")
        return ModelConfig(
            model=config.llm_model,
            api_key_env="LLM_API_KEY",
            api_base=None,
            max_tokens=config.llm_max_tokens_per_agent,
            temperature=0.7,
        )

    def get_model_info(self) -> dict[str, Any]:
        """Return info about available models for the dashboard."""
        return {
            "available_models": list(self._available_models.keys()),
            "routing_table": {
                tt.value: [m for m in prefs if m in self._available_models]
                for tt, prefs in TASK_ROUTING.items()
            },
        }


# Singleton
_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    """Get (or create) the global model router."""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
