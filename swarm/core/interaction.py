"""
Component 4: Interaction Protocol (No Central Router)

Agents discover and respond to each other's outputs through the shared
environment. All communication goes through artifacts — NO direct
agent-to-agent messaging.

This module provides:
  - Discovery: find relevant artifacts for a task
  - Dependency resolution: wait for needed artifacts
  - Conflict detection: detect when agents produce conflicting work
  - Sub-task spawning helpers
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from swarm.models.artifact import Artifact, ArtifactType
from swarm.models.task import Task, TaskPriority, TaskType

if TYPE_CHECKING:
    from swarm.core.environment import Environment
    from swarm.core.task_queue import TaskQueue

logger = logging.getLogger("swarm.interaction")

# Maps task types to the artifact types and tags they typically need
TASK_DEPENDENCY_MAP: dict[TaskType, dict[str, Any]] = {
    TaskType.PLAN_ARCHITECTURE: {
        "needs_types": [ArtifactType.REQUIREMENTS_DOC],
        "needs_tags": ["requirements"],
    },
    TaskType.DESIGN_DATABASE: {
        "needs_types": [ArtifactType.ARCHITECTURE_PLAN],
        "needs_tags": ["architecture", "database"],
    },
    TaskType.CREATE_API: {
        "needs_types": [ArtifactType.ARCHITECTURE_PLAN, ArtifactType.DATABASE_SCHEMA],
        "needs_tags": ["architecture", "database_schema"],
    },
    TaskType.BUILD_FRONTEND_COMPONENT: {
        "needs_types": [ArtifactType.API_SPEC, ArtifactType.UI_DESIGN],
        "needs_tags": ["api_spec"],
    },
    TaskType.REVIEW_CODE: {
        "needs_types": [ArtifactType.CODE_FILE],
        "needs_tags": [],
    },
    TaskType.WRITE_TESTS: {
        "needs_types": [ArtifactType.CODE_FILE, ArtifactType.API_SPEC],
        "needs_tags": [],
    },
    TaskType.DEPLOY: {
        "needs_types": [ArtifactType.CODE_FILE],
        "needs_tags": [],
    },
    TaskType.DEBUG: {
        "needs_types": [ArtifactType.BUG_REPORT, ArtifactType.CODE_FILE],
        "needs_tags": [],
    },
    TaskType.FIX_CODE: {
        "needs_types": [ArtifactType.REVIEW],
        "needs_tags": [],
    },
}


class InteractionProtocol:
    def __init__(self, environment: Environment, task_queue: TaskQueue) -> None:
        self.environment = environment
        self.task_queue = task_queue

    # ── Discovery ─────────────────────────────────────────────────

    async def discover_relevant_artifacts(
        self,
        project_id: str,
        task: Task,
    ) -> list[dict[str, Any]]:
        """
        Find artifacts relevant to this task using:
          1. Explicit payload references (source_artifact_id)
          2. Task-type dependency map
          3. Tag-based search from task payload
        """
        artifacts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Explicit reference in payload
        source_id = task.payload.get("source_artifact_id")
        if source_id:
            art = await self.environment.get_artifact(source_id)
            if art and art["id"] not in seen_ids:
                artifacts.append(art)
                seen_ids.add(art["id"])

        arch_id = task.payload.get("architecture_artifact_id")
        if arch_id:
            art = await self.environment.get_artifact(arch_id)
            if art and art["id"] not in seen_ids:
                artifacts.append(art)
                seen_ids.add(art["id"])

        review_id = task.payload.get("review_artifact_id")
        if review_id:
            art = await self.environment.get_artifact(review_id)
            if art and art["id"] not in seen_ids:
                artifacts.append(art)
                seen_ids.add(art["id"])

        # 2. Dependency map
        dep_info = TASK_DEPENDENCY_MAP.get(task.type, {})
        for art_type in dep_info.get("needs_types", []):
            results = await self.environment.query_artifacts(
                project_id, artifact_type=art_type.value
            )
            for r in results:
                if r["id"] not in seen_ids:
                    artifacts.append(r)
                    seen_ids.add(r["id"])

        for tag in dep_info.get("needs_tags", []):
            results = await self.environment.query_artifacts(project_id, tags=[tag])
            for r in results:
                if r["id"] not in seen_ids:
                    artifacts.append(r)
                    seen_ids.add(r["id"])

        # 3. Component-specific search
        component = task.payload.get("component", "")
        if component:
            results = await self.environment.query_artifacts(
                project_id, tags=[component.lower().replace(" ", "_")]
            )
            for r in results:
                if r["id"] not in seen_ids:
                    artifacts.append(r)
                    seen_ids.add(r["id"])

        logger.info(
            f"Discovery for task {task.id} ({task.type.value}): "
            f"found {len(artifacts)} relevant artifacts"
        )
        return artifacts

    # ── Conflict detection ────────────────────────────────────────

    async def detect_conflicts(
        self,
        project_id: str,
        new_artifact: Artifact,
    ) -> list[dict[str, Any]]:
        """
        Check if the new artifact conflicts with existing artifacts.
        Conflicts: same type + overlapping tags from different agents.
        """
        existing = await self.environment.query_artifacts(
            project_id, artifact_type=new_artifact.type.value
        )
        conflicts = []
        for existing_art in existing:
            if existing_art["agent_id"] == new_artifact.agent_id:
                continue
            if existing_art["id"] == new_artifact.id:
                continue
            # Check tag overlap
            existing_tags = set(existing_art.get("tags", []))
            new_tags = set(new_artifact.tags)
            overlap = existing_tags & new_tags
            if overlap:
                conflicts.append({
                    "existing_artifact": existing_art,
                    "overlapping_tags": list(overlap),
                })

        if conflicts:
            logger.warning(
                f"Conflict detected: artifact '{new_artifact.name}' conflicts with "
                f"{len(conflicts)} existing artifact(s)"
            )
        return conflicts

    async def spawn_conflict_resolution(
        self,
        project_id: str,
        artifact_a: dict,
        artifact_b: dict,
        parent_task_id: str | None = None,
    ) -> None:
        """Spawn a resolve_conflict task when two artifacts conflict."""
        task = Task(
            type=TaskType.RESOLVE_CONFLICT,
            payload={
                "artifact_a_id": artifact_a["id"],
                "artifact_a_name": artifact_a["name"],
                "artifact_b_id": artifact_b["id"],
                "artifact_b_name": artifact_b["name"],
                "conflict_type": artifact_a["type"],
            },
            priority=TaskPriority.HIGH,
            project_id=project_id,
            parent_task_id=parent_task_id,
        )
        await self.task_queue.submit(task)
        logger.info(
            f"Conflict resolution spawned: '{artifact_a['name']}' vs '{artifact_b['name']}'"
        )

    # ── Sub-task spawning helpers ─────────────────────────────────

    async def spawn_subtask(
        self,
        task_type: TaskType,
        project_id: str,
        payload: dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        parent_task_id: str | None = None,
        spawned_by_agent_id: str | None = None,
        dependencies: list[str] | None = None,
    ) -> Task:
        """Helper for agents to spawn sub-tasks."""
        task = Task(
            type=task_type,
            payload=payload,
            priority=priority,
            project_id=project_id,
            parent_task_id=parent_task_id,
            spawned_by_agent_id=spawned_by_agent_id,
            dependencies=dependencies or [],
        )
        await self.task_queue.submit(task)
        return task

    async def spawn_integration_test_if_ready(self, project_id: str) -> bool:
        """
        Check if all code components are done + reviewed.
        If so, spawn an integration_test task.
        """
        code_artifacts = await self.environment.query_artifacts(
            project_id, artifact_type=ArtifactType.CODE_FILE.value
        )
        review_artifacts = await self.environment.query_artifacts(
            project_id, artifact_type=ArtifactType.REVIEW.value
        )

        if not code_artifacts:
            return False

        # Check that all reviews passed (no outstanding issues)
        reviews_with_issues = [
            r for r in review_artifacts
            if r.get("metadata", {}).get("has_issues", False)
        ]
        if reviews_with_issues:
            return False

        # Check that we have at least as many reviews as code artifacts
        if len(review_artifacts) < len(code_artifacts):
            return False

        # All good — spawn integration test
        task = Task(
            type=TaskType.INTEGRATION_TEST,
            payload={
                "trigger": "all_components_reviewed",
                "code_artifact_count": len(code_artifacts),
                "review_artifact_count": len(review_artifacts),
            },
            priority=TaskPriority.HIGH,
            project_id=project_id,
        )
        await self.task_queue.submit(task)
        logger.info(f"Integration test spawned for project {project_id}")
        return True
