"""
Agent identity model — defines who an agent is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from swarm.models.task import TaskType


class AgentStatus(StrEnum):
    ALIVE = "alive"
    WORKING = "working"
    WAITING = "waiting"
    DEAD = "dead"


# Task type → persona mapping
TASK_PERSONA_MAP: dict[TaskType, tuple[str, str]] = {
    TaskType.ANALYZE_REQUIREMENTS: ("Product Manager", "product_manager"),
    TaskType.PLAN_ARCHITECTURE: ("System Architect", "architect"),
    TaskType.DESIGN_DATABASE: ("Database Engineer", "backend_engineer"),
    TaskType.DESIGN_UI: ("UI/UX Designer", "designer"),
    TaskType.CREATE_API: ("Backend Engineer", "backend_engineer"),
    TaskType.WRITE_CODE: ("Software Engineer", "backend_engineer"),
    TaskType.BUILD_FRONTEND_COMPONENT: ("Frontend Engineer", "frontend_engineer"),
    TaskType.REVIEW_CODE: ("Tech Lead", "reviewer"),
    TaskType.WRITE_TESTS: ("QA Engineer", "tester"),
    TaskType.WRITE_DOCS: ("Technical Writer", "product_manager"),
    TaskType.RESEARCH: ("Research Analyst", "researcher"),
    TaskType.DEBUG: ("Debug Specialist", "backend_engineer"),
    TaskType.DEPLOY: ("DevOps Engineer", "devops"),
    TaskType.FIX_CODE: ("Software Engineer", "backend_engineer"),
    TaskType.INTEGRATION_TEST: ("QA Engineer", "tester"),
    TaskType.RESOLVE_CONFLICT: ("Tech Lead", "reviewer"),
    # New: Hive Computer spec
    TaskType.EVALUATE_PROJECT: ("Project Evaluator", "evaluator"),
    TaskType.ASSEMBLE_DELIVERABLES: ("Delivery Engineer", "integrator"),
    TaskType.GENERATE_MEDIA: ("Media Designer", "designer"),
    TaskType.COUNCIL_REVIEW: ("Council Moderator", "reviewer"),
}


@dataclass
class AgentIdentity:
    task_type: TaskType
    project_id: str
    task_id: str
    id: str = field(default_factory=lambda: f"agent-{uuid.uuid4().hex[:12]}")
    status: AgentStatus = AgentStatus.ALIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    died_at: datetime | None = None

    @property
    def persona(self) -> str:
        return TASK_PERSONA_MAP.get(self.task_type, ("Software Engineer", "backend_engineer"))[0]

    @property
    def role(self) -> str:
        return TASK_PERSONA_MAP.get(self.task_type, ("Software Engineer", "backend_engineer"))[1]

    @property
    def name(self) -> str:
        short_id = self.id.split("-")[-1][:6]
        return f"{self.persona} ({short_id})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "persona": self.persona,
            "role": self.role,
            "name": self.name,
            "status": self.status.value,
            "personality": {"task_type": self.task_type.value},
            "created_at": self.created_at.isoformat(),
            "died_at": self.died_at.isoformat() if self.died_at else None,
        }
