"""
Component 3: Per-Agent Memory

Each agent has its OWN memory:
  - Short-term (Redis, TTL-based): working context, recent reasoning, capped at 20 items
  - Long-term (PostgreSQL + pgvector): expertise, patterns, decisions with semantic search

Memory is assembled into the LLM system prompt before every call.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from swarm.config import config

if TYPE_CHECKING:
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.agent_memory")

# Lazy-loaded embedding model
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer(config.embedding_model)
            logger.info(f"Embedding model loaded: {config.embedding_model}")
        except ImportError:
            logger.warning("sentence-transformers not installed — long-term memory search disabled")
    return _embed_model


def embed_text(text: str) -> list[float] | None:
    model = _get_embed_model()
    if model is None:
        return None
    return model.encode(text).tolist()


class AgentMemory:
    """Per-agent memory manager."""

    SHORT_TERM_TTL = 600  # 10 minutes
    MAX_SHORT_TERM = 20

    def __init__(
        self,
        agent_id: str,
        project_id: str,
        redis: RedisClient,
        db: PostgresDB,
    ) -> None:
        self.agent_id = agent_id
        self.project_id = project_id
        self.redis = redis
        self.db = db
        self._prefix = f"agent_mem:{agent_id}"

    # ── Short-term memory (Redis) ─────────────────────────────────

    async def remember(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store a short-term memory item."""
        redis_key = f"{self._prefix}:{key}"
        await self.redis.set_json(redis_key, value, ttl=ttl or self.SHORT_TERM_TTL)

    async def recall_short(self, key: str) -> Any | None:
        """Recall a specific short-term memory."""
        return await self.redis.get_json(f"{self._prefix}:{key}")

    async def append_reasoning(self, step: dict[str, Any]) -> None:
        """Append a reasoning step to the chain (capped at MAX_SHORT_TERM)."""
        await self.redis.lpush_json(
            f"{self._prefix}:reasoning_chain",
            step,
            max_len=self.MAX_SHORT_TERM,
        )

    async def get_reasoning_chain(self) -> list[dict]:
        """Get recent reasoning steps."""
        return await self.redis.lrange_json(f"{self._prefix}:reasoning_chain")

    async def set_working_artifact(self, artifact_data: dict) -> None:
        """Store the artifact being worked on."""
        await self.remember("working_artifact", artifact_data)

    async def get_working_artifact(self) -> dict | None:
        return await self.recall_short("working_artifact")

    # ── Long-term memory (PostgreSQL + pgvector) ──────────────────

    async def memorize(self, content: str, tags: list[str] | None = None) -> None:
        """Store a long-term memory with vector embedding."""
        embedding = embed_text(content)
        await self.db.store_memory({
            "id": str(uuid.uuid4()),
            "project_id": self.project_id,
            "agent_id": self.agent_id,
            "content": content,
            "tags": tags or [],
            "embedding": str(embedding) if embedding else None,
        })
        logger.debug(f"Agent {self.agent_id} memorized: {content[:80]}...")

    async def recall(self, query: str, k: int = 5) -> list[dict]:
        """Semantic search over long-term memories."""
        embedding = embed_text(query)
        if embedding is None:
            return []
        results = await self.db.search_memories(self.project_id, embedding, k=k)
        return results

    # ── Context window assembly ───────────────────────────────────

    async def get_context_window(self, task_summary: str, env_context: dict | None = None) -> str:
        """
        Assemble full context for LLM call:
          1. Project context (from environment)
          2. Long-term memories (top 5 semantically similar)
          3. Short-term reasoning chain (last 10)
          4. Working artifact state
        """
        parts: list[str] = []

        # 1. Project context
        if env_context:
            parts.append("## Current Project State")
            project = env_context.get("project", {})
            if project:
                parts.append(f"Project: {project.get('name', 'Unknown')}")
                parts.append(f"Brief: {project.get('brief', '')}")
            summary = env_context.get("artifact_summary", {})
            if summary:
                parts.append("\nExisting artifacts:")
                for atype, names in summary.items():
                    parts.append(f"  - {atype}: {', '.join(names[:5])}")
            task_counts = env_context.get("task_counts", {})
            if task_counts:
                parts.append(f"\nTask status: {task_counts}")
            parts.append("")

        # 2. Long-term memories
        memories = await self.recall(task_summary, k=5)
        if memories:
            parts.append("## Relevant Memories (from past work on this project)")
            for m in memories:
                parts.append(f"- {m.get('content', '')}")
            parts.append("")

        # 3. Short-term reasoning chain
        chain = await self.get_reasoning_chain()
        if chain:
            parts.append("## Recent Reasoning")
            for step in chain[:10]:
                action = step.get("action", "")
                result = step.get("result", "")
                parts.append(f"- {action}: {result}")
            parts.append("")

        # 4. Working artifact
        working = await self.get_working_artifact()
        if working:
            parts.append("## Current Working Artifact")
            parts.append(f"Name: {working.get('name', '')}")
            content_preview = working.get("content", "")[:500]
            parts.append(f"Content (preview): {content_preview}")
            parts.append("")

        return "\n".join(parts)

    # ── Cleanup ───────────────────────────────────────────────────

    async def forget_short_term(self) -> None:
        """Clear all short-term memory (called when agent dies)."""
        keys = [
            f"{self._prefix}:reasoning_chain",
            f"{self._prefix}:working_artifact",
        ]
        for key in keys:
            await self.redis.delete_key(key)
        logger.debug(f"Short-term memory cleared for agent {self.agent_id}")
