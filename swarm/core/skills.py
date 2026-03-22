"""
Skills & Template System — reusable workflow templates.

A Skill is a named, reusable workflow definition that can be:
- Created from a completed project (save as template)
- Created manually with a task graph definition
- Instantiated as a new project with pre-configured tasks
- Shared across the workspace

Built-in skills provide common workflows out of the box.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from swarm.models.task import Task, TaskPriority, TaskType

if TYPE_CHECKING:
    from swarm.core.task_queue import TaskQueue
    from swarm.db.postgres import PostgresDB

logger = logging.getLogger("swarm.skills")


# Built-in skill templates
BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "id": "skill-market-research",
        "name": "Market Research",
        "description": "Research competitors, market size, and positioning for a product idea",
        "category": "research",
        "workflow": {
            "tasks": [
                {"type": "research", "payload": {"brief": "Conduct market research: competitors, market size, trends, and positioning"}, "priority": 3},
                {"type": "write_docs", "payload": {"brief": "Write a market research report from the research findings"}, "priority": 2},
            ]
        },
        "input_fields": [
            {"name": "product_idea", "label": "Product/Idea", "type": "text", "required": True},
            {"name": "target_market", "label": "Target Market", "type": "text", "required": False},
        ],
        "builtin": True,
    },
    {
        "id": "skill-full-stack-app",
        "name": "Full-Stack Application",
        "description": "Build a complete full-stack application from a product brief",
        "category": "build",
        "workflow": {
            "tasks": [
                {"type": "analyze_requirements", "priority": 3},
                {"type": "plan_architecture", "priority": 3},
                # Architecture decomposition handles the rest via reactive triggers
            ]
        },
        "input_fields": [
            {"name": "brief", "label": "Product Brief", "type": "textarea", "required": True},
        ],
        "builtin": True,
    },
    {
        "id": "skill-api-design",
        "name": "API Design & Build",
        "description": "Design and implement a REST API from requirements",
        "category": "build",
        "workflow": {
            "tasks": [
                {"type": "analyze_requirements", "priority": 3},
                {"type": "design_database", "priority": 2},
                {"type": "create_api", "priority": 2},
                {"type": "write_tests", "priority": 1},
                {"type": "write_docs", "priority": 1},
            ]
        },
        "input_fields": [
            {"name": "brief", "label": "API Requirements", "type": "textarea", "required": True},
        ],
        "builtin": True,
    },
    {
        "id": "skill-code-review",
        "name": "Code Review & Audit",
        "description": "Review code for quality, security, and best practices",
        "category": "quality",
        "workflow": {
            "tasks": [
                {"type": "review_code", "priority": 3},
                {"type": "write_tests", "priority": 2},
            ]
        },
        "input_fields": [
            {"name": "code", "label": "Code to Review", "type": "textarea", "required": True},
            {"name": "focus", "label": "Review Focus", "type": "text", "required": False},
        ],
        "builtin": True,
    },
    {
        "id": "skill-daily-summary",
        "name": "Daily Summary Report",
        "description": "Generate a daily summary report — great for scheduled recurring tasks",
        "category": "monitoring",
        "workflow": {
            "tasks": [
                {"type": "research", "payload": {"brief": "Gather latest updates and metrics"}, "priority": 2},
                {"type": "write_docs", "payload": {"brief": "Compile a concise daily summary report"}, "priority": 2},
            ]
        },
        "input_fields": [
            {"name": "topic", "label": "What to Monitor", "type": "text", "required": True},
            {"name": "sources", "label": "Sources/URLs", "type": "textarea", "required": False},
        ],
        "builtin": True,
    },
    {
        "id": "skill-landing-page",
        "name": "Landing Page",
        "description": "Design and build a landing page with copy, UI, and code",
        "category": "build",
        "workflow": {
            "tasks": [
                {"type": "analyze_requirements", "priority": 3},
                {"type": "design_ui", "priority": 2},
                {"type": "build_frontend_component", "priority": 2},
                {"type": "write_docs", "priority": 1},
            ]
        },
        "input_fields": [
            {"name": "brief", "label": "Landing Page Brief", "type": "textarea", "required": True},
            {"name": "brand", "label": "Brand/Style Notes", "type": "text", "required": False},
        ],
        "builtin": True,
    },
]


class SkillRegistry:
    """Manages built-in and user-created skills."""

    def __init__(self, db: PostgresDB) -> None:
        self.db = db

    async def list_skills(self, category: str | None = None) -> list[dict[str, Any]]:
        """List all available skills (built-in + user-created)."""
        skills = list(BUILTIN_SKILLS)

        try:
            user_skills = await self.db.get_skills(category=category)
            skills.extend(user_skills)
        except Exception:
            pass

        if category:
            skills = [s for s in skills if s.get("category") == category]

        return skills

    async def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        """Get a single skill by ID."""
        for s in BUILTIN_SKILLS:
            if s["id"] == skill_id:
                return s

        try:
            return await self.db.get_skill(skill_id)
        except Exception:
            return None

    async def create_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        """Create a new user skill."""
        skill_id = skill.get("id") or f"skill-{uuid.uuid4().hex[:12]}"
        skill["id"] = skill_id
        skill["created_at"] = datetime.now(timezone.utc).isoformat()
        skill["builtin"] = False

        await self.db.create_skill(skill)
        logger.info(f"Skill created: {skill_id} — {skill.get('name', '')}")
        return skill

    async def create_skill_from_project(self, project_id: str) -> dict[str, Any]:
        """Save a completed project as a reusable skill template."""
        project = await self.db.get_project(project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")

        tasks = await self.db.get_tasks(project_id)

        # Extract the task graph (only root-level tasks, not reactive children)
        root_tasks = [t for t in tasks if not t.get("parent_task_id")]
        task_defs = []
        for t in root_tasks:
            task_defs.append({
                "type": t["type"],
                "priority": t.get("priority", 1),
            })

        skill = {
            "name": f"Template: {project.get('name', 'Untitled')}",
            "description": f"Generated from project '{project.get('name', '')}'",
            "category": "custom",
            "source_project_id": project_id,
            "workflow": {"tasks": task_defs},
            "input_fields": [
                {"name": "brief", "label": "Project Brief", "type": "textarea", "required": True},
            ],
        }

        return await self.create_skill(skill)

    async def instantiate_skill(
        self, skill_id: str, inputs: dict[str, str], task_queue: TaskQueue
    ) -> dict[str, Any]:
        """Create a new project from a skill template."""
        from swarm.models.project import Project

        skill = await self.get_skill(skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not found")

        # Create project
        brief = inputs.get("brief", "") or inputs.get("product_idea", "") or str(inputs)
        project = Project(
            name=inputs.get("name", skill.get("name", "Skill Project")),
            brief=brief,
            config={"skill_id": skill_id, "skill_inputs": inputs},
        )
        await self.db.create_project(project.to_dict())

        # Submit the workflow tasks
        workflow = skill.get("workflow", {})
        task_defs = workflow.get("tasks", [])

        submitted = []
        for i, td in enumerate(task_defs):
            task_type_str = td.get("type", "research")
            try:
                task_type = TaskType(task_type_str)
            except ValueError:
                task_type = TaskType.RESEARCH

            payload = dict(td.get("payload", {}))
            payload.update(inputs)
            payload["skill_id"] = skill_id
            payload["project_name"] = project.name
            if "brief" not in payload:
                payload["brief"] = brief

            task = Task(
                type=task_type,
                payload=payload,
                priority=TaskPriority(td.get("priority", 2)),
                project_id=project.id,
            )
            await task_queue.submit(task)
            submitted.append(task.id)

        logger.info(f"Skill {skill_id} instantiated: project={project.id}, tasks={len(submitted)}")
        return {
            "project_id": project.id,
            "project_name": project.name,
            "skill_id": skill_id,
            "tasks_created": len(submitted),
        }
