"""
Project model — a product brief that the swarm builds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class Project:
    name: str
    brief: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: ProjectStatus = ProjectStatus.ACTIVE
    config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_tokens_used: int = 0
    total_cost: float = 0.0
    agent_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "brief": self.brief,
            "status": self.status.value,
            "config": self.config,
            "created_at": self.created_at.isoformat(),
            "total_tokens_used": self.total_tokens_used,
            "total_cost": self.total_cost,
            "agent_count": self.agent_count,
        }
