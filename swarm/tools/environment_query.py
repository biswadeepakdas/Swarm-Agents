"""
Environment query tool — agents can query the shared environment for artifacts.
Thin wrapper that provides a tool-like interface for the LLM to use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.core.environment import Environment

logger = logging.getLogger("swarm.tools.env_query")


class EnvironmentQueryTool:
    def __init__(self, environment: Environment, project_id: str) -> None:
        self.environment = environment
        self.project_id = project_id

    async def find_by_type(self, artifact_type: str) -> list[dict[str, Any]]:
        """Find all artifacts of a given type."""
        return await self.environment.query_artifacts(
            self.project_id, artifact_type=artifact_type
        )

    async def find_by_tags(self, tags: list[str]) -> list[dict[str, Any]]:
        """Find artifacts matching any of the given tags."""
        return await self.environment.query_artifacts(
            self.project_id, tags=tags
        )

    async def get_project_summary(self) -> dict[str, Any]:
        """Get a full project state snapshot."""
        return await self.environment.get_project_state(self.project_id)

    async def get_artifact_content(self, artifact_id: str) -> str | None:
        """Get the content of a specific artifact."""
        art = await self.environment.get_artifact(artifact_id)
        return art["content"] if art else None
