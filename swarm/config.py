"""
Swarm configuration — loaded from environment variables.
"""

import os
from dataclasses import dataclass, field


@dataclass
class SwarmConfig:
    # Database
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/swarm",
        )
    )
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379")
    )

    # LLM (via litellm)
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic")
    )
    llm_api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    )
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    )
    llm_max_tokens_per_agent: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS_PER_AGENT", "8000"))
    )
    llm_max_budget_per_project: float = field(
        default_factory=lambda: float(os.getenv("LLM_MAX_BUDGET_PER_PROJECT", "5.00"))
    )

    # Embeddings
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    )

    # Swarm
    max_concurrency: int = field(
        default_factory=lambda: int(os.getenv("SWARM_MAX_CONCURRENCY", "10"))
    )
    max_agents_per_project: int = field(
        default_factory=lambda: int(os.getenv("SWARM_MAX_AGENTS_PER_PROJECT", "50"))
    )
    task_retry_limit: int = field(
        default_factory=lambda: int(os.getenv("SWARM_TASK_RETRY_LIMIT", "3"))
    )
    agent_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("SWARM_AGENT_TIMEOUT_SECONDS", "300"))
    )

    # Redis Stream keys
    task_stream: str = "swarm:tasks"
    task_group: str = "swarm-workers"
    dead_letter_stream: str = "swarm:dead_letters"
    pubsub_channel: str = "swarm:events"


config = SwarmConfig()
