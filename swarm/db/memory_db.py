"""
In-memory database backend for testing without PostgreSQL.
Drop-in replacement for PostgresDB — same interface, stores everything in dicts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("swarm.db.memory")


class MemoryDB:
    """In-memory implementation of the PostgresDB interface."""

    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.artifacts: dict[str, dict] = {}
        self.memories: list[dict] = []
        self._scheduled_tasks: dict[str, dict] = {}
        self._skills: dict[str, dict] = {}
        self._council_sessions: dict[str, dict] = {}
        self.pool = self  # Fake pool for API routes that access pool.acquire()

    async def connect(self) -> None:
        logger.info("MemoryDB connected (in-memory mode)")

    async def close(self) -> None:
        logger.info("MemoryDB closed")

    async def run_migrations(self) -> None:
        logger.info("MemoryDB: no migrations needed")

    # Fake pool context manager for routes that do `async with db.pool.acquire()`
    def acquire(self):
        return _FakeConnection(self)

    # ── Project CRUD ──────────────────────────────────────────

    async def create_project(self, project: dict[str, Any]) -> dict[str, Any]:
        pid = str(project["id"])
        self.projects[pid] = {
            **project,
            "created_at": project.get("created_at") or datetime.now(timezone.utc),
        }
        return self.projects[pid]

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.projects.get(str(project_id))

    async def get_projects(self) -> list[dict[str, Any]]:
        return list(self.projects.values())

    async def update_project(self, project_id: str, **kwargs: Any) -> None:
        pid = str(project_id)
        if pid in self.projects:
            self.projects[pid].update(kwargs)

    async def delete_project(self, project_id: str) -> None:
        pid = str(project_id)
        self.projects.pop(pid, None)
        self.tasks = {k: v for k, v in self.tasks.items() if v.get("project_id") != pid}
        self.agents = {k: v for k, v in self.agents.items() if v.get("project_id") != pid}
        self.artifacts = {k: v for k, v in self.artifacts.items() if v.get("project_id") != pid}

    async def cleanup_stale_on_startup(self) -> dict[str, int]:
        zombie_agents = 0
        for a in self.agents.values():
            if a.get("status") in ("alive", "working"):
                a["status"] = "dead"
                a["death_cause"] = "server_restart"
                zombie_agents += 1
        zombie_tasks = 0
        for t in self.tasks.values():
            if t.get("status") == "active":
                t["status"] = "dead"
                t["error"] = "Server restarted"
                zombie_tasks += 1
        return {"zombie_agents": zombie_agents, "zombie_tasks": zombie_tasks}

    # ── Task CRUD ─────────────────────────────────────────────

    async def create_task(self, task: dict[str, Any]) -> None:
        tid = task["id"]
        # Handle payload that might be a JSON string
        payload = task.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
        self.tasks[tid] = {
            **task,
            "payload": payload,
            "created_at": task.get("created_at") or datetime.now(timezone.utc),
        }

    async def update_task(self, task_id: str, **kwargs: Any) -> None:
        if task_id in self.tasks:
            for k, v in kwargs.items():
                if k in ("payload", "result") and isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        pass
                self.tasks[task_id][k] = v

    async def get_tasks(self, project_id: str, status: str | None = None) -> list[dict]:
        pid = str(project_id)
        results = []
        for t in self.tasks.values():
            if str(t.get("project_id")) == pid:
                if status is None or t.get("status") == status:
                    results.append(t)
        # Sort by priority desc, then created_at
        results.sort(key=lambda x: (-int(x.get("priority", 1)), str(x.get("created_at", ""))))
        return results

    # ── Agent CRUD ────────────────────────────────────────────

    async def create_agent(self, agent: dict[str, Any]) -> None:
        self.agents[agent["id"]] = {
            **agent,
            "created_at": agent.get("created_at") or datetime.now(timezone.utc),
        }

    async def update_agent(self, agent_id: str, **kwargs: Any) -> None:
        if agent_id in self.agents:
            self.agents[agent_id].update(kwargs)

    async def get_agents(self, project_id: str) -> list[dict]:
        pid = str(project_id)
        results = [a for a in self.agents.values() if str(a.get("project_id")) == pid]
        results.sort(key=lambda x: str(x.get("created_at", "")))
        return results

    # ── Artifact CRUD ─────────────────────────────────────────

    async def create_artifact(self, artifact: dict[str, Any]) -> None:
        aid = str(artifact["id"])
        self.artifacts[aid] = {
            **artifact,
            "created_at": artifact.get("created_at") or datetime.now(timezone.utc),
        }

    async def query_artifacts(
        self,
        project_id: str,
        artifact_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        pid = str(project_id)
        results = []
        for a in self.artifacts.values():
            if str(a.get("project_id")) != pid:
                continue
            if artifact_type and a.get("type") != artifact_type:
                continue
            if tags:
                art_tags = set(a.get("tags", []))
                if not art_tags.intersection(set(tags)):
                    continue
            results.append(a)
        results.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return results

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.artifacts.get(str(artifact_id))

    # ── Scheduled Task CRUD ──────────────────────────────────

    async def create_scheduled_task(self, task: dict[str, Any]) -> dict[str, Any]:
        tid = str(task["id"])
        task.setdefault("created_at", datetime.now(timezone.utc))
        task.setdefault("run_count", 0)
        task.setdefault("enabled", True)
        self._scheduled_tasks[tid] = task
        return task

    async def get_scheduled_tasks(self, enabled_only: bool = False) -> list[dict]:
        tasks = list(self._scheduled_tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.get("enabled")]
        return tasks

    async def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        return self._scheduled_tasks.get(str(task_id))

    async def update_scheduled_task(self, task_id: str, **kwargs: Any) -> None:
        tid = str(task_id)
        if tid in self._scheduled_tasks:
            self._scheduled_tasks[tid].update(kwargs)

    async def delete_scheduled_task(self, task_id: str) -> None:
        self._scheduled_tasks.pop(str(task_id), None)

    # ── Skill CRUD ──────────────────────────────────────────

    async def create_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        sid = str(skill["id"])
        skill.setdefault("created_at", datetime.now(timezone.utc))
        skill.setdefault("usage_count", 0)
        self._skills[sid] = skill
        return skill

    async def get_skills(self, category: str | None = None) -> list[dict]:
        skills = list(self._skills.values())
        if category:
            skills = [s for s in skills if s.get("category") == category]
        return skills

    async def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        return self._skills.get(str(skill_id))

    async def increment_skill_usage(self, skill_id: str) -> None:
        sid = str(skill_id)
        if sid in self._skills:
            self._skills[sid]["usage_count"] = self._skills[sid].get("usage_count", 0) + 1

    # ── Council CRUD ────────────────────────────────────────

    async def create_council_session(self, session: dict[str, Any]) -> dict[str, Any]:
        sid = str(session["id"])
        session.setdefault("created_at", datetime.now(timezone.utc))
        self._council_sessions[sid] = session
        return session

    async def get_council_sessions(self, project_id: str | None = None) -> list[dict]:
        sessions = list(self._council_sessions.values())
        if project_id:
            sessions = [s for s in sessions if str(s.get("project_id")) == str(project_id)]
        sessions.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        return sessions

    # ── Memory CRUD ───────────────────────────────────────────

    async def store_memory(self, memory: dict[str, Any]) -> None:
        self.memories.append(memory)

    async def search_memories(
        self, project_id: str, embedding: list[float], k: int = 5
    ) -> list[dict]:
        # No vector search in memory mode — return most recent memories for this project
        pid = str(project_id)
        relevant = [m for m in self.memories if str(m.get("project_id")) == pid]
        return relevant[-k:]


class _FakeConnection:
    """Fake async context manager for pool.acquire()."""

    def __init__(self, db: MemoryDB):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, query: str, *args) -> list[dict]:
        # Handle the list-projects query from routes.py
        if "FROM projects" in query:
            rows = sorted(
                self.db.projects.values(),
                key=lambda x: str(x.get("created_at", "")),
                reverse=True,
            )
            return rows[:50]
        return []
