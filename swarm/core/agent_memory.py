"""
Component 3: Per-Agent Memory

Each agent has its OWN memory:
  - Short-term (Redis, TTL-based): working context, recent reasoning, capped at 20 items
  - Long-term (PostgreSQL + pgvector): expertise, patterns, decisions with semantic search

Memory is assembled into the LLM system prompt before every call.

Embedding backends (tried in order):
  1. NVIDIA NIM API (free, 1024-dim → truncated to 384)
  2. OpenAI embeddings API
  3. sentence-transformers (local, if installed)
  4. No embeddings (memory still stored, just not searchable)
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from swarm.config import config

if TYPE_CHECKING:
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.agent_memory")

# Target embedding dimension (must match pgvector column: vector(384))
EMBED_DIM = 384

# Cache the embedding backend so we don't re-detect every call
_embed_backend: str | None = None
_embed_model: Any = None


async def _embed_nvidia(text: str) -> list[float] | None:
    """Use NVIDIA NIM embedding API (free tier)."""
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://integrate.api.nvidia.com/v1/embeddings",
                json={
                    "input": [text[:2000]],  # truncate to fit
                    "model": "nvidia/nv-embedqa-e5-v5",
                    "input_type": "query",
                    "encoding_format": "float",
                    "truncate": "END",
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data["data"][0]["embedding"]
            # Truncate/pad to EMBED_DIM
            if len(embedding) > EMBED_DIM:
                embedding = embedding[:EMBED_DIM]
            elif len(embedding) < EMBED_DIM:
                embedding += [0.0] * (EMBED_DIM - len(embedding))
            return embedding
    except Exception as e:
        logger.warning(f"NVIDIA embedding failed: {e}")
        return None


async def _embed_openai(text: str) -> list[float] | None:
    """Use OpenAI embedding API."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                json={
                    "input": text[:2000],
                    "model": "text-embedding-3-small",
                    "dimensions": EMBED_DIM,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"OpenAI embedding failed: {e}")
        return None


def _embed_local(text: str) -> list[float] | None:
    """Use local sentence-transformers (if installed)."""
    global _embed_model
    try:
        if _embed_model is None:
            from sentence_transformers import SentenceTransformer
            _embed_model = SentenceTransformer(config.embedding_model)
            logger.info(f"Local embedding model loaded: {config.embedding_model}")
        return _embed_model.encode(text).tolist()
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"Local embedding failed: {e}")
        return None


async def embed_text(text: str) -> list[float] | None:
    """
    Generate an embedding for text using the best available backend.
    Returns a list of floats with dimension EMBED_DIM, or None.
    """
    global _embed_backend

    # Try backends in priority order
    if _embed_backend in (None, "nvidia"):
        result = await _embed_nvidia(text)
        if result:
            _embed_backend = "nvidia"
            return result

    if _embed_backend in (None, "openai"):
        result = await _embed_openai(text)
        if result:
            _embed_backend = "openai"
            return result

    if _embed_backend in (None, "local"):
        result = _embed_local(text)
        if result:
            _embed_backend = "local"
            return result

    if _embed_backend is None:
        _embed_backend = "none"
        logger.warning("No embedding backend available — long-term memory search disabled")

    return None


# Synchronous wrapper for backwards compat
def embed_text_sync(text: str) -> list[float] | None:
    """Synchronous embedding — only uses local model."""
    return _embed_local(text)


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
        embedding = await embed_text(content)
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
        """Semantic search over long-term memories for this project."""
        embedding = await embed_text(query)
        if embedding is None:
            # Fallback: return recent memories by tag
            return await self._recall_by_tags(k)
        results = await self.db.search_memories(self.project_id, embedding, k=k)
        return results

    async def recall_cross_project(self, query: str, k: int = 3) -> list[dict]:
        """Search memories across ALL projects — for cross-project learning."""
        embedding = await embed_text(query)
        if embedding is None:
            return []
        return await self.db.search_memories_global(embedding, k=k)

    async def _recall_by_tags(self, k: int = 5) -> list[dict]:
        """Fallback memory recall — returns recent memories (no vector search)."""
        try:
            async with self.db.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE project_id = $1 ORDER BY created_at DESC LIMIT $2",
                    self.project_id, k,
                )
                return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Context window assembly ───────────────────────────────────

    async def get_context_window(self, task_summary: str, env_context: dict | None = None) -> str:
        """
        Assemble full context for LLM call:
          1. Project context (from environment)
          2. Long-term memories (top 5 semantically similar)
          3. Cross-project memories (top 3 from other projects)
          4. Short-term reasoning chain (last 10)
          5. Working artifact state
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

        # 2. Long-term memories (this project)
        memories = await self.recall(task_summary, k=5)
        if memories:
            parts.append("## Relevant Memories (from past work on this project)")
            for m in memories:
                parts.append(f"- {m.get('content', '')}")
            parts.append("")

        # 3. Cross-project memories
        try:
            cross_memories = await self.recall_cross_project(task_summary, k=3)
            # Filter out memories from this project (already included above)
            cross_memories = [m for m in cross_memories if str(m.get('project_id', '')) != self.project_id]
            if cross_memories:
                parts.append("## Insights from Other Projects")
                for m in cross_memories:
                    parts.append(f"- {m.get('content', '')}")
                parts.append("")
        except Exception:
            pass  # Cross-project search is optional

        # 4. Short-term reasoning chain
        chain = await self.get_reasoning_chain()
        if chain:
            parts.append("## Recent Reasoning")
            for step in chain[:10]:
                action = step.get("action", "")
                result = step.get("result", "")
                parts.append(f"- {action}: {result}")
            parts.append("")

        # 5. Working artifact
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
