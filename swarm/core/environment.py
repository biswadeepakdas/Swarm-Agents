"""
Component 2: Shared Environment / State Store

Replaces the central router. Agents don't talk to each other directly — they
read/write to the shared environment. Other agents discover relevant outputs
through tag-based, dependency-based, and event-based discovery.

Reactive triggers auto-submit new tasks when patterns are detected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from swarm.models.artifact import Artifact, ArtifactType
from swarm.models.task import Task, TaskPriority, TaskType

if TYPE_CHECKING:
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient
    from swarm.core.task_queue import TaskQueue

logger = logging.getLogger("swarm.environment")


# Reactive trigger rules: artifact type → task(s) to auto-submit
REACTIVE_TRIGGERS: dict[ArtifactType, list[dict[str, Any]]] = {
    ArtifactType.CODE_FILE: [
        {"type": TaskType.REVIEW_CODE, "priority": TaskPriority.HIGH, "label": "Auto-review"},
    ],
    ArtifactType.API_SPEC: [
        {"type": TaskType.WRITE_TESTS, "priority": TaskPriority.NORMAL, "label": "Auto-test for API spec"},
    ],
    ArtifactType.BUG_REPORT: [
        {"type": TaskType.DEBUG, "priority": TaskPriority.HIGH, "label": "Auto-debug"},
    ],
    ArtifactType.FRONTEND_COMPONENT: [
        {"type": TaskType.REVIEW_CODE, "priority": TaskPriority.NORMAL, "label": "Auto-review frontend"},
    ],
    ArtifactType.REVIEW: [],  # handled specially — check for issues
    ArtifactType.ARCHITECTURE_PLAN: [],  # handled specially — decompose into build tasks
}


class Environment:
    def __init__(self, db: PostgresDB, redis: RedisClient) -> None:
        self.db = db
        self.redis = redis
        self.task_queue: TaskQueue | None = None

    def set_task_queue(self, tq: TaskQueue) -> None:
        self.task_queue = tq

    # ── Publish artifact (agent output goes here) ─────────────────

    async def publish_artifact(self, artifact: Artifact) -> None:
        """
        Agent publishes output to the shared environment.
        Triggers reactive rules and notifies other agents via pub/sub.
        """
        await self.db.create_artifact(artifact.to_dict())

        await self.redis.publish_event({
            "type": "artifact_created",
            "artifact_id": artifact.id,
            "artifact_type": artifact.type.value,
            "artifact_name": artifact.name,
            "agent_id": artifact.agent_id,
            "project_id": artifact.project_id,
            "tags": artifact.tags,
        })

        logger.info(
            f"Artifact published: {artifact.name} ({artifact.type.value}) "
            f"by agent {artifact.agent_id} [tags={artifact.tags}]"
        )

        # Fire reactive triggers
        await self._fire_reactive_triggers(artifact)

        # Check if any WAITING tasks are now unblocked
        await self._check_unblocked_tasks(artifact)

    # ── Query artifacts (agents discover each other's work) ───────

    async def query_artifacts(
        self,
        project_id: str,
        artifact_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.db.query_artifacts(project_id, artifact_type, tags)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return await self.db.get_artifact(artifact_id)

    # ── Project state snapshot (for agent context injection) ──────

    async def get_project_state(self, project_id: str) -> dict[str, Any]:
        """Full snapshot of current project state — injected into agent context."""
        project = await self.db.get_project(project_id)
        artifacts = await self.db.query_artifacts(project_id)
        tasks = await self.db.get_tasks(project_id)
        agents = await self.db.get_agents(project_id)

        # Summarize artifacts by type
        artifact_summary: dict[str, list[str]] = {}
        for a in artifacts:
            atype = a["type"]
            if atype not in artifact_summary:
                artifact_summary[atype] = []
            artifact_summary[atype].append(a["name"])

        # Count task statuses
        task_counts: dict[str, int] = {}
        for t in tasks:
            s = t["status"]
            task_counts[s] = task_counts.get(s, 0) + 1

        return {
            "project": project,
            "artifact_summary": artifact_summary,
            "artifact_count": len(artifacts),
            "task_counts": task_counts,
            "total_tasks": len(tasks),
            "total_agents": len(agents),
            "active_agents": sum(1 for a in agents if a["status"] in ("alive", "working")),
        }

    # ── Dependency checking ───────────────────────────────────────

    async def check_blockers(self, task: Task) -> list[str]:
        """
        Check if a task's dependencies are met.
        Returns list of unmet dependency tags (empty = all met).
        """
        if not task.dependencies:
            return []

        unmet = []
        for dep_tag in task.dependencies:
            artifacts = await self.db.query_artifacts(task.project_id, tags=[dep_tag])
            if not artifacts:
                unmet.append(dep_tag)
        return unmet

    # ── Reactive triggers ─────────────────────────────────────────

    async def _fire_reactive_triggers(self, artifact: Artifact) -> None:
        if not self.task_queue:
            return

        # Standard triggers from the map
        triggers = REACTIVE_TRIGGERS.get(artifact.type, [])
        for trigger in triggers:
            new_task = Task(
                type=trigger["type"],
                payload={
                    "trigger": "reactive",
                    "source_artifact_id": artifact.id,
                    "source_artifact_name": artifact.name,
                    "source_artifact_type": artifact.type.value,
                    "label": trigger.get("label", ""),
                },
                priority=trigger["priority"],
                project_id=artifact.project_id,
                parent_task_id=artifact.task_id,
                spawned_by_agent_id=artifact.agent_id,
            )
            await self.task_queue.submit(new_task)
            logger.info(
                f"Reactive trigger: {artifact.type.value} → {trigger['type'].value} "
                f"({trigger.get('label', '')})"
            )

        # Special: review with issues → spawn fix task
        if artifact.type == ArtifactType.REVIEW:
            metadata = artifact.metadata or {}
            if metadata.get("has_issues", False):
                fix_task = Task(
                    type=TaskType.FIX_CODE,
                    payload={
                        "trigger": "reactive",
                        "review_artifact_id": artifact.id,
                        "issues": metadata.get("issues", []),
                    },
                    priority=TaskPriority.HIGH,
                    project_id=artifact.project_id,
                    parent_task_id=artifact.task_id,
                    spawned_by_agent_id=artifact.agent_id,
                )
                await self.task_queue.submit(fix_task)
                logger.info("Reactive trigger: review with issues → fix_code")

        # Special: architecture plan → decompose into build tasks
        if artifact.type == ArtifactType.ARCHITECTURE_PLAN:
            metadata = artifact.metadata or {}
            components = metadata.get("components", [])
            if components:
                tasks_to_submit = []
                for comp in components:
                    comp_type = comp.get("type", "write_code")
                    try:
                        task_type = TaskType(comp_type)
                    except ValueError:
                        task_type = TaskType.WRITE_CODE

                    new_task = Task(
                        type=task_type,
                        payload={
                            "trigger": "architecture_decomposition",
                            "component": comp.get("name", ""),
                            "description": comp.get("description", ""),
                            "architecture_artifact_id": artifact.id,
                        },
                        priority=TaskPriority(comp.get("priority", TaskPriority.NORMAL)),
                        project_id=artifact.project_id,
                        parent_task_id=artifact.task_id,
                        spawned_by_agent_id=artifact.agent_id,
                        dependencies=comp.get("dependencies", []),
                    )
                    tasks_to_submit.append(new_task)

                if tasks_to_submit:
                    await self.task_queue.submit_batch(tasks_to_submit)
                    logger.info(
                        f"Architecture decomposition: {len(tasks_to_submit)} build tasks spawned"
                    )

    async def _check_unblocked_tasks(self, artifact: Artifact) -> None:
        """
        When a new artifact arrives, check if any WAITING tasks are now unblocked.
        If so, re-queue them.
        """
        if not self.task_queue:
            return

        waiting_tasks = await self.db.get_tasks(artifact.project_id, status="waiting")
        for task_row in waiting_tasks:
            deps = task_row.get("dependencies", [])
            if not deps:
                continue

            # Check if any of the artifact's tags satisfy a dependency
            if any(tag in artifact.tags for tag in deps):
                # Re-check all deps
                remaining = []
                for dep_tag in deps:
                    found = await self.db.query_artifacts(artifact.project_id, tags=[dep_tag])
                    if not found:
                        remaining.append(dep_tag)

                if not remaining:
                    # All deps met — re-queue
                    task = Task.from_dict({
                        **task_row,
                        "payload": task_row.get("payload", {}),
                        "created_at": task_row["created_at"].isoformat() if hasattr(task_row["created_at"], "isoformat") else task_row["created_at"],
                        "started_at": None,
                        "completed_at": None,
                    })
                    task.status = TaskStatus.PENDING
                    await self.db.update_task(task.id, status="pending")
                    await self.redis.submit_task(task.to_dict())
                    logger.info(f"Task {task.id} unblocked — re-queued")

    # ── Check completion ──────────────────────────────────────────

    async def check_project_completion(self, project_id: str) -> bool:
        """Check if all tasks for a project are done."""
        tasks = await self.db.get_tasks(project_id)
        if not tasks:
            return False
        return all(t["status"] in ("completed", "dead") for t in tasks)
