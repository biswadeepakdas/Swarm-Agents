"""
Task model — the unit of work that triggers agent spawning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any


class TaskType(StrEnum):
    ANALYZE_REQUIREMENTS = "analyze_requirements"
    PLAN_ARCHITECTURE = "plan_architecture"
    DESIGN_DATABASE = "design_database"
    DESIGN_UI = "design_ui"
    CREATE_API = "create_api"
    WRITE_CODE = "write_code"
    BUILD_FRONTEND_COMPONENT = "build_frontend_component"
    REVIEW_CODE = "review_code"
    WRITE_TESTS = "write_tests"
    WRITE_DOCS = "write_docs"
    RESEARCH = "research"
    DEBUG = "debug"
    DEPLOY = "deploy"
    FIX_CODE = "fix_code"
    INTEGRATION_TEST = "integration_test"
    RESOLVE_CONFLICT = "resolve_conflict"
    # New: Perplexity Computer spec
    EVALUATE_PROJECT = "evaluate_project"
    ASSEMBLE_DELIVERABLES = "assemble_deliverables"
    GENERATE_MEDIA = "generate_media"
    COUNCIL_REVIEW = "council_review"


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING = "waiting"  # blocked on dependency
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"  # exceeded retry limit


def _parse_dt(val: Any) -> datetime | None:
    """Safely parse a datetime from various formats (str, datetime, None, 'None')."""
    if val is None or val == "None" or val == "null" or val == "":
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


class TaskPriority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class Task:
    type: TaskType
    payload: dict[str, Any] = field(default_factory=dict)
    priority: TaskPriority = TaskPriority.NORMAL
    project_id: str = ""
    id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    parent_task_id: str | None = None
    spawned_by_agent_id: str | None = None
    assigned_agent_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    dependencies: list[str] = field(default_factory=list)  # artifact tags needed
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "payload": self.payload,
            "priority": self.priority.value,
            "project_id": self.project_id,
            "parent_task_id": self.parent_task_id,
            "spawned_by_agent_id": self.spawned_by_agent_id,
            "assigned_agent_id": self.assigned_agent_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "dependencies": self.dependencies,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=data["id"],
            type=TaskType(data["type"]),
            payload=data.get("payload", {}),
            priority=TaskPriority(int(data.get("priority", 1))),
            project_id=data.get("project_id", ""),
            parent_task_id=data.get("parent_task_id"),
            spawned_by_agent_id=data.get("spawned_by_agent_id"),
            assigned_agent_id=data.get("assigned_agent_id"),
            status=TaskStatus(data.get("status", "pending")),
            result=data.get("result"),
            error=data.get("error"),
            retry_count=int(data.get("retry_count", 0)),
            dependencies=data.get("dependencies", []),
            created_at=_parse_dt(data.get("created_at")) or datetime.now(timezone.utc),
            started_at=_parse_dt(data.get("started_at")),
            completed_at=_parse_dt(data.get("completed_at")),
        )
