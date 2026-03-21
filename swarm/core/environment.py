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
from swarm.models.task import Task, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient
    from swarm.core.task_queue import TaskQueue

logger = logging.getLogger("swarm.environment")


# Reactive trigger rules: artifact type → task(s) to auto-submit
# This is THE chain that drives the entire swarm.
# requirements_doc → architecture → decompose → build/design/test → review
#
# IMPORTANT: Triggers that can loop (code→review→fix→code) are guarded
# by depth tracking in _fire_reactive_triggers() below.
REACTIVE_TRIGGERS: dict[ArtifactType, list[dict[str, Any]]] = {
    # ── Stage 1: Requirements → Architecture ──
    ArtifactType.REQUIREMENTS_DOC: [
        {"type": TaskType.PLAN_ARCHITECTURE, "priority": TaskPriority.CRITICAL, "label": "Architecture from requirements"},
    ],

    # architecture_plan is handled specially below (decompose into build tasks)
    ArtifactType.ARCHITECTURE_PLAN: [],

    # ── Stage 2: Build artifacts → one round of review ──
    # Code file → review (depth-limited to prevent code→review→fix→code loop)
    ArtifactType.CODE_FILE: [
        {"type": TaskType.REVIEW_CODE, "priority": TaskPriority.HIGH, "label": "Auto-review code"},
    ],

    # Database schema → trigger API creation
    ArtifactType.DATABASE_SCHEMA: [
        {"type": TaskType.CREATE_API, "priority": TaskPriority.HIGH, "label": "API from database schema"},
    ],

    # Frontend component → review
    ArtifactType.FRONTEND_COMPONENT: [
        {"type": TaskType.REVIEW_CODE, "priority": TaskPriority.NORMAL, "label": "Auto-review frontend"},
    ],

    # UI design → build frontend component
    ArtifactType.UI_DESIGN: [
        {"type": TaskType.BUILD_FRONTEND_COMPONENT, "priority": TaskPriority.HIGH, "label": "Build UI from design"},
    ],

    # ── Terminal nodes — NO further triggers to prevent infinite loops ──
    ArtifactType.REVIEW: [],         # handled specially below (issue → fix, max 1 round)
    ArtifactType.TEST_SUITE: [],     # terminal
    ArtifactType.DOCUMENTATION: [],  # terminal
    ArtifactType.DEPLOYMENT_CONFIG: [],  # terminal
    ArtifactType.DECISION: [],       # terminal
    ArtifactType.BUG_REPORT: [],     # terminal
    ArtifactType.API_SPEC: [],       # terminal
}

# Max depth for reactive trigger chains to prevent infinite loops
# depth 0 = original build task, depth 1 = review/test triggered by build → STOP
# The code→review→fix→review infinite loop was killing the swarm.
# One round of review is enough — if the review finds issues, they stay as notes.
MAX_TRIGGER_DEPTH = 1


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

    async def _get_trigger_depth(self, artifact: Artifact) -> int:
        """
        Get the current trigger depth from the task that produced this artifact.
        Uses trigger_depth stored in task payload — no DB walks needed.
        """
        try:
            # Look up the task that produced this artifact
            tasks = await self.db.get_tasks(artifact.project_id)
            for t in tasks:
                if str(t.get("id", "")) == str(artifact.task_id):
                    payload = t.get("payload", {})
                    if isinstance(payload, str):
                        import json as _json
                        try:
                            payload = _json.loads(payload)
                        except Exception:
                            payload = {}
                    return int(payload.get("trigger_depth", 0))
        except Exception:
            pass
        return 0

    async def _fire_reactive_triggers(self, artifact: Artifact) -> None:
        if not self.task_queue:
            return

        # ── Depth check: prevent infinite trigger loops ──
        # code(0) → review(1) → fix(2) → STOP. No more reviews after a fix.
        depth = await self._get_trigger_depth(artifact)
        if depth >= MAX_TRIGGER_DEPTH:
            logger.info(
                f"Trigger depth {depth} >= {MAX_TRIGGER_DEPTH} for {artifact.type.value}. "
                f"Stopping reactive chain to prevent infinite loop."
            )
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
                    "trigger_depth": depth + 1,
                },
                priority=trigger["priority"],
                project_id=artifact.project_id,
                parent_task_id=artifact.task_id,
                spawned_by_agent_id=artifact.agent_id,
            )
            await self.task_queue.submit(new_task)
            logger.info(
                f"Reactive trigger (depth={depth+1}): {artifact.type.value} → "
                f"{trigger['type'].value} ({trigger.get('label', '')})"
            )

        # Special: review with issues → spawn fix task (only if depth allows)
        if artifact.type == ArtifactType.REVIEW and depth < MAX_TRIGGER_DEPTH:
            metadata = artifact.metadata or {}
            if metadata.get("has_issues", False):
                fix_task = Task(
                    type=TaskType.FIX_CODE,
                    payload={
                        "trigger": "reactive",
                        "review_artifact_id": artifact.id,
                        "issues": metadata.get("issues", []),
                        "trigger_depth": depth + 1,
                    },
                    priority=TaskPriority.HIGH,
                    project_id=artifact.project_id,
                    parent_task_id=artifact.task_id,
                    spawned_by_agent_id=artifact.agent_id,
                )
                await self.task_queue.submit(fix_task)
                logger.info(f"Reactive trigger (depth={depth+1}): review with issues → fix_code")

        # Special: architecture plan → decompose into build tasks
        if artifact.type == ArtifactType.ARCHITECTURE_PLAN:
            metadata = artifact.metadata or {}
            components = metadata.get("components", [])

            # ── FALLBACK: If LLM didn't produce parseable components,
            # always spawn a sensible default set of build tasks ──
            if not components:
                logger.warning(
                    "Architecture plan had no COMPONENTS JSON. "
                    "Spawning default build tasks as fallback."
                )
                components = [
                    {"name": "Database Schema", "type": "design_database",
                     "description": "Design database tables and relationships from the architecture plan",
                     "priority": 3},
                    {"name": "Backend API", "type": "create_api",
                     "description": "Create the REST API endpoints described in the architecture",
                     "priority": 2},
                    {"name": "UI Design", "type": "design_ui",
                     "description": "Design the user interface and screens from the architecture plan",
                     "priority": 2},
                    {"name": "Core Business Logic", "type": "write_code",
                     "description": "Implement core business logic and services",
                     "priority": 2},
                    {"name": "Deployment Config", "type": "deploy",
                     "description": "Create deployment configuration and infrastructure setup",
                     "priority": 1},
                ]

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
