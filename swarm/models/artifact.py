"""
Artifact model — everything agents produce goes here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def _parse_dt(val) -> datetime | None:
    if val is None or val == "None" or val == "null" or val == "":
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


class ArtifactType(StrEnum):
    CODE_FILE = "code_file"
    ARCHITECTURE_PLAN = "architecture_plan"
    API_SPEC = "api_spec"
    DATABASE_SCHEMA = "database_schema"
    UI_DESIGN = "ui_design"
    TEST_SUITE = "test_suite"
    DOCUMENTATION = "documentation"
    DECISION = "decision"
    REVIEW = "review"
    BUG_REPORT = "bug_report"
    DEPLOYMENT_CONFIG = "deployment_config"
    REQUIREMENTS_DOC = "requirements_doc"
    FRONTEND_COMPONENT = "frontend_component"


@dataclass
class Artifact:
    project_id: str
    task_id: str
    agent_id: str
    type: ArtifactType
    name: str
    content: str
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # artifact IDs
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "type": self.type.value,
            "name": self.name,
            "content": self.content,
            "tags": self.tags,
            "dependencies": self.dependencies,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        return cls(
            id=data["id"],
            project_id=data["project_id"],
            task_id=data["task_id"],
            agent_id=data["agent_id"],
            type=ArtifactType(data["type"]),
            name=data["name"],
            content=data["content"],
            tags=data.get("tags", []),
            dependencies=data.get("dependencies", []),
            metadata=data.get("metadata", {}),
            created_at=_parse_dt(data.get("created_at")) or datetime.now(timezone.utc),
        )
