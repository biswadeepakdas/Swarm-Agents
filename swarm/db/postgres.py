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
        migrations_dir = Path(__file__).parent.parent / "migrations"
        migration_files = sorted(migrations_dir.glob("*.sql"))
        async with self.pool.acquire() as conn:
            for mf in migration_files:
                sql = mf.read_text()
                try:
                    await conn.execute(sql)
                    logger.info(f"Migration applied: {mf.name}")
                except Exception as e:
                    # IF NOT EXISTS handles most cases — log and continue
                    logger.debug(f"Migration {mf.name} note: {e}")
        logger.info(f"All migrations applied ({len(migration_files)} files)")

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

    async def get_projects(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM projects ORDER BY created_at DESC")
            return [dict(r) for r in rows]

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

    async def cleanup_stale_on_startup(self) -> dict[str, int]:
        """Mark zombie agents as dead and zombie tasks as failed.
        Called on startup AND periodically by the watchdog.
        Kills agents/tasks stuck for more than 3 minutes."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Kill zombie agents (alive/working for more than 3 min)
                r1 = await conn.execute(
                    "UPDATE agents SET status = 'dead', died_at = NOW() "
                    "WHERE status IN ('alive', 'working') "
                    "AND created_at < NOW() - INTERVAL '3 minutes'"
                )
                zombie_agents = int(r1.split()[-1]) if r1 else 0

                # Fail zombie tasks (active for more than 3 min)
                r2 = await conn.execute(
                    "UPDATE tasks SET status = 'dead', error = 'Agent timed out (watchdog)' "
                    "WHERE status = 'active' "
                    "AND started_at < NOW() - INTERVAL '3 minutes'"
                )
                zombie_tasks = int(r2.split()[-1]) if r2 else 0

        if zombie_agents or zombie_tasks:
            logger.warning(
                f"Cleanup: killed {zombie_agents} zombie agents, "
                f"failed {zombie_tasks} zombie tasks"
            )
        return {"zombie_agents": zombie_agents, "zombie_tasks": zombie_tasks}

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

    # ── Interaction CRUD ────────────────────────────────────────────

    async def create_interaction(self, interaction: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO interactions (id, project_id, task_id, agent_id, question, options, context, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                interaction["id"],
                interaction["project_id"],
                interaction.get("task_id"),
                interaction.get("agent_id"),
                interaction["question"],
                interaction.get("options", []),
                interaction.get("context", ""),
                interaction.get("status", "pending"),
            )
            return dict(row)

    async def get_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM interactions WHERE id = $1", interaction_id)
            return dict(row) if row else None

    async def get_pending_interactions(self, project_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM interactions WHERE project_id = $1 AND status = 'pending' ORDER BY created_at",
                project_id,
            )
            return [dict(r) for r in rows]

    async def answer_interaction(self, interaction_id: str, response: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE interactions SET response = $1, status = 'answered', answered_at = NOW()
                WHERE id = $2 AND status = 'pending'
                RETURNING *
                """,
                response,
                interaction_id,
            )
            return dict(row) if row else None

    async def expire_interactions(self, task_id: str) -> None:
        """Expire all pending interactions for a task (e.g., on timeout)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE interactions SET status = 'expired' WHERE task_id = $1 AND status = 'pending'",
                task_id,
            )

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
                WHERE project_id = $2 AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                embedding_str,
                project_id,
                k,
            )
            return [dict(r) for r in rows]

    async def search_memories_global(
        self, embedding: list[float], k: int = 5
    ) -> list[dict]:
        """Search memories across ALL projects — for cross-project learning."""
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *, embedding <=> $1::vector AS distance
                FROM memories
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $2
                """,
                embedding_str,
                k,
            )
            return [dict(r) for r in rows]

    # ── Scheduled Task CRUD ────────────────────────────────────────

    async def create_scheduled_task(self, task: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO scheduled_tasks (id, name, description, project_id, trigger_type,
                    cron_expression, workflow, status, next_run_at, max_runs)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                task.get("id") or str(__import__("uuid").uuid4()),
                task["name"],
                task.get("description", ""),
                task.get("project_id"),
                task.get("trigger_type", "cron"),
                task.get("cron_expression", ""),
                json.dumps(task.get("workflow", {})),
                task.get("status", "active"),
                task.get("next_run_at"),
                task.get("max_runs"),
            )
            return dict(row)

    async def get_scheduled_tasks(self, status: str | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM scheduled_tasks WHERE status = $1 ORDER BY created_at DESC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
                )
            return [dict(r) for r in rows]

    async def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM scheduled_tasks WHERE id = $1", task_id)
            return dict(row) if row else None

    async def update_scheduled_task(self, task_id: str, **kwargs: Any) -> None:
        sets = []
        vals = []
        idx = 1
        for key, val in kwargs.items():
            if key == "workflow" and isinstance(val, dict):
                val = json.dumps(val)
            sets.append(f"{key} = ${idx}")
            vals.append(val)
            idx += 1
        vals.append(task_id)
        sql = f"UPDATE scheduled_tasks SET {', '.join(sets)} WHERE id::text = ${idx}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *vals)

    async def delete_scheduled_task(self, task_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM scheduled_tasks WHERE id::text = $1", task_id)

    # ── Skill CRUD ─────────────────────────────────────────────────

    async def create_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO skills (id, name, description, category, workflow, input_fields,
                    source_project_id, builtin)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    workflow = EXCLUDED.workflow,
                    input_fields = EXCLUDED.input_fields
                RETURNING *
                """,
                skill["id"],
                skill["name"],
                skill.get("description", ""),
                skill.get("category", "custom"),
                json.dumps(skill.get("workflow", {})),
                json.dumps(skill.get("input_fields", [])),
                skill.get("source_project_id"),
                skill.get("builtin", False),
            )
            return dict(row)

    async def get_skills(self, category: str | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if category:
                rows = await conn.fetch(
                    "SELECT * FROM skills WHERE category = $1 ORDER BY usage_count DESC",
                    category,
                )
            else:
                rows = await conn.fetch("SELECT * FROM skills ORDER BY usage_count DESC")
            return [dict(r) for r in rows]

    async def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM skills WHERE id = $1", skill_id)
            return dict(row) if row else None

    async def increment_skill_usage(self, skill_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE skills SET usage_count = usage_count + 1 WHERE id = $1",
                skill_id,
            )

    # ── Council Session CRUD ──────────────────────────────────────

    async def create_council_session(self, session: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO council_sessions (project_id, question, context, votes,
                    synthesis, agreement_score, chosen_approach, reasoning, total_latency_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                session.get("project_id"),
                session["question"],
                session.get("context", ""),
                json.dumps(session.get("votes", [])),
                session.get("synthesis", ""),
                session.get("agreement_score", 0),
                session.get("chosen_approach", ""),
                session.get("reasoning", ""),
                session.get("total_latency_ms", 0),
            )
            return dict(row)

    async def get_council_sessions(self, project_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM council_sessions WHERE project_id = $1 ORDER BY created_at DESC",
                project_id,
            )
            return [dict(r) for r in rows]
