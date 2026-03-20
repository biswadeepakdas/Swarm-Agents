"""
REST API routes for the swarm engine.

POST /api/projects          — Submit a product brief, starts the swarm
GET  /api/projects/{id}     — Get project status + all artifacts
GET  /api/projects/{id}/agents    — List all agents (alive + dead)
GET  /api/projects/{id}/tasks     — Task queue status
GET  /api/projects/{id}/artifacts — All produced artifacts
POST /api/projects/{id}/inject    — Inject a new requirement mid-build
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from swarm.models.project import Project
from swarm.models.task import Task, TaskPriority, TaskType

logger = logging.getLogger("swarm.api")
router = APIRouter(prefix="/api")

# These will be set at app startup
_db = None
_redis = None
_task_queue = None
_environment = None


def init_routes(db, redis, task_queue, environment):
    global _db, _redis, _task_queue, _environment
    _db = db
    _redis = redis
    _task_queue = task_queue
    _environment = environment


# ── Request/Response models ───────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    brief: str = Field(..., min_length=10, max_length=10000)
    config: dict[str, Any] = Field(default_factory=dict)


class InjectRequirementRequest(BaseModel):
    requirement: str = Field(..., min_length=5, max_length=5000)
    priority: int = Field(default=2, ge=0, le=3)


class ProjectResponse(BaseModel):
    id: str
    name: str
    brief: str
    status: str
    config: dict
    created_at: str
    agent_count: int = 0
    artifact_count: int = 0
    task_counts: dict[str, int] = {}


# ── Endpoints ─────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    """List all projects."""
    async with _db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM projects ORDER BY created_at DESC LIMIT 50")
    return {
        "projects": [_serialize_record(dict(r)) for r in rows],
    }


@router.post("/projects", response_model=dict)
async def create_project(req: CreateProjectRequest):
    """Submit a product brief. The swarm starts building immediately."""
    project = Project(name=req.name, brief=req.brief, config=req.config)
    await _db.create_project(project.to_dict())

    # Submit the initial task: analyze_requirements
    initial_task = Task(
        type=TaskType.ANALYZE_REQUIREMENTS,
        payload={"brief": req.brief, "project_name": req.name},
        priority=TaskPriority.CRITICAL,
        project_id=project.id,
    )
    await _task_queue.submit(initial_task)

    logger.info(f"Project created: {project.id} — '{project.name}'")

    return {
        "id": project.id,
        "name": project.name,
        "status": "active",
        "message": "Swarm activated. First agent (Product Manager) spawning to analyze requirements.",
        "initial_task_id": initial_task.id,
    }


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    """Get project status with summary."""
    project = await _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    state = await _environment.get_project_state(project_id)
    return {
        "project": _serialize_project(project),
        "artifact_summary": state.get("artifact_summary", {}),
        "task_counts": state.get("task_counts", {}),
        "total_tasks": state.get("total_tasks", 0),
        "total_agents": state.get("total_agents", 0),
        "active_agents": state.get("active_agents", 0),
    }


@router.get("/projects/{project_id}/agents")
async def get_agents(project_id: str):
    """List all agents spawned for this project."""
    project = await _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    agents = await _db.get_agents(project_id)
    return {
        "project_id": project_id,
        "total": len(agents),
        "alive": sum(1 for a in agents if a["status"] in ("alive", "working")),
        "agents": [_serialize_record(a) for a in agents],
    }


@router.get("/projects/{project_id}/tasks")
async def get_tasks(project_id: str, status: str | None = None):
    """Get task queue status."""
    project = await _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    tasks = await _db.get_tasks(project_id, status=status)
    return {
        "project_id": project_id,
        "total": len(tasks),
        "tasks": [_serialize_record(t) for t in tasks],
    }


@router.get("/projects/{project_id}/artifacts")
async def get_artifacts(project_id: str, artifact_type: str | None = None):
    """Get all artifacts produced by the swarm."""
    project = await _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    artifacts = await _db.query_artifacts(project_id, artifact_type=artifact_type)
    return {
        "project_id": project_id,
        "total": len(artifacts),
        "artifacts": [_serialize_record(a) for a in artifacts],
    }


@router.post("/projects/{project_id}/inject")
async def inject_requirement(project_id: str, req: InjectRequirementRequest):
    """God's-eye view: inject a new requirement or change mid-build."""
    project = await _db.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    task = Task(
        type=TaskType.ANALYZE_REQUIREMENTS,
        payload={
            "brief": req.requirement,
            "injection": True,
            "original_project_brief": project.get("brief", ""),
        },
        priority=TaskPriority(req.priority),
        project_id=project_id,
    )
    await _task_queue.submit(task)

    return {
        "message": "Requirement injected. A new agent will analyze and adapt.",
        "task_id": task.id,
    }


@router.get("/health")
async def health():
    return {"status": "ok", "engine": "swarm-agents", "version": "0.1.0"}


# ── Helpers ───────────────────────────────────────────────────

def _serialize_project(row: dict) -> dict:
    return {k: str(v) if hasattr(v, "isoformat") else v for k, v in row.items()}


def _serialize_record(row: dict) -> dict:
    result = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
        elif isinstance(v, (list, dict)):
            result[k] = v
        else:
            result[k] = str(v) if v is not None else None
    return result
