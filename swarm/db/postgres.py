"""
Async PostgreSQL connection pool and operations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import asyncpg

from swarm.config import config

logger = logging.getLogger("swarm.db.postgres")


class PostgresDB:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        dsn = config.database_url
        # Normalize DSN for asyncpg (accepts postgresql:// only)
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        dsn = dsn.replace("postgres://", "postgresql://")  # Railway uses postgres://
        self.pool = await asyncpg.create_pool(dsn, min_size=2, max_size=20)
        logger.info("PostgreSQL pool connected")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL pool closed")

    async def run_migrations(self) -> None:
        migration_file = Path(__file__).parent.parent / "migrations" / "001_initial.sql"
        sql = migration_file.read_text()
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("Migrations applied")

    # ── Project CRUD ──────────────────────────────────────────────

    async def create_project(self, project: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (id, name, brief, status, config)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                project["id"],
                project["name"],
                project["brief"],
                project.get("status", "active"),
                json.dumps(project.get("config", {})),
            )
            return dict(row)

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
            return dict(row) if row else None

    async def update_project(self, project_id: str, **kwargs: Any) -> None:
        sets = []
        vals = []
        idx = 1
        for key, val in kwargs.items():
            sets.append(f"{key} = ${idx}")
            vals.append(val)
            idx += 1
        vals.append(project_id)
        sql = f"UPDATE projects SET {', '.join(sets)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *vals)

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and all its related data (tasks, agents, artifacts)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM artifacts WHERE project_id = $1", project_id)
                await conn.execute("DELETE FROM agents WHERE project_id = $1", project_id)
                await conn.execute("DELETE FROM tasks WHERE project_id = $1", project_id)
                await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        logger.info(f"Project {project_id} and all related data deleted")

    # ── Task CRUD ─────────────────────────────────────────────────

    async def create_task(self, task: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tasks (id, project_id, type, payload, priority, status,
                    parent_task_id, spawned_by_agent_id, dependencies)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload
                """,
                task["id"],
                task["project_id"],
                task["type"],
                json.dumps(task.get("payload", {})),
                task.get("priority", 1),
                task.get("status", "pending"),
                task.get("parent_task_id"),
                task.get("spawned_by_agent_id"),
                task.get("dependencies", []),
            )

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        sets = []
        vals = []
        idx = 1
        for key, val in kwargs.items():
            if key in ("payload", "result") and isinstance(val, dict):
                val = json.dumps(val)
            sets.append(f"{key} = ${idx}")
            vals.append(val)
            idx += 1
        vals.append(task_id)
        sql = f"UPDATE tasks SET {', '.join(sets)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *vals)

    async def get_tasks(self, project_id: str, status: str | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM tasks WHERE project_id = $1 AND status = $2 ORDER BY priority DESC, created_at",
                    project_id, status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM tasks WHERE project_id = $1 ORDER BY priority DESC, created_at",
                    project_id,
                )
            return [dict(r) for r in rows]

    # ── Agent CRUD ────────────────────────────────────────────────

    async def create_agent(self, agent: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agents (id, project_id, task_id, persona, role, name, personality, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                agent["id"],
                agent["project_id"],
                agent["task_id"],
                agent["persona"],
                agent["role"],
                agent["name"],
                json.dumps(agent.get("personality", {})),
                agent.get("status", "alive"),
            )

    async def update_agent(self, agent_id: str, **kwargs: Any) -> None:
        sets = []
        vals = []
        idx = 1
        for key, val in kwargs.items():
            sets.append(f"{key} = ${idx}")
            vals.append(val)
            idx += 1
        vals.append(agent_id)
        sql = f"UPDATE agents SET {', '.join(sets)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *vals)

    async def get_agents(self, project_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agents WHERE project_id = $1 ORDER BY created_at",
                project_id,
            )
            return [dict(r) for r in rows]

    # ── Artifact CRUD ─────────────────────────────────────────────

    async def create_artifact(self, artifact: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO artifacts (id, project_id, task_id, agent_id, type, name, content, tags, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    tags = EXCLUDED.tags,
                    metadata = EXCLUDED.metadata
                """,
                artifact["id"],
                artifact["project_id"],
                artifact["task_id"],
                artifact["agent_id"],
                artifact["type"],
                artifact["name"],
                artifact["content"],
                artifact.get("tags", []),
                json.dumps(artifact.get("metadata", {})),
            )

    async def query_artifacts(
        self,
        project_id: str,
        artifact_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        conditions = ["project_id = $1"]
        vals: list[Any] = [project_id]
        idx = 2
        if artifact_type:
            conditions.append(f"type = ${idx}")
            vals.append(artifact_type)
            idx += 1
        if tags:
            conditions.append(f"tags && ${idx}")
            vals.append(tags)
            idx += 1
        sql = f"SELECT * FROM artifacts WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *vals)
            return [dict(r) for r in rows]

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM artifacts WHERE id = $1", artifact_id)
            return dict(row) if row else None

    # ── Memory CRUD ───────────────────────────────────────────────

    async def store_memory(self, memory: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memories (id, project_id, agent_id, content, tags, embedding)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                memory["id"],
                memory["project_id"],
                memory.get("agent_id"),
                memory["content"],
                memory.get("tags", []),
                memory.get("embedding"),
            )

    async def search_memories(
        self, project_id: str, embedding: list[float], k: int = 5
    ) -> list[dict]:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *, embedding <=> $1::vector AS distance
                FROM memories
                WHERE project_id = $2
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                embedding_str,
                project_id,
                k,
            )
            return [dict(r) for r in rows]
