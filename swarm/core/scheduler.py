"""
Task Scheduler — cron-like recurring task support.

Supports one-shot scheduled tasks and recurring tasks with cron expressions.
Runs as a background loop alongside the spawn loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from swarm.models.task import Task, TaskPriority, TaskType

if TYPE_CHECKING:
    from swarm.core.task_queue import TaskQueue
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.scheduler")


def _parse_cron(expression: str) -> dict[str, Any]:
    """Parse a simplified cron expression: minute hour day_of_month month day_of_week.
    Supports: *, */N, N, N-M, N,M,O
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: expected 5 fields, got {len(parts)}")

    fields = ["minute", "hour", "day", "month", "weekday"]
    result = {}
    for i, (name, part) in enumerate(zip(fields, parts)):
        result[name] = part
    return result


def _cron_matches(expression: str, dt: datetime) -> bool:
    """Check if a datetime matches a cron expression."""
    try:
        parsed = _parse_cron(expression)
    except ValueError:
        return False

    checks = [
        ("minute", dt.minute, 0, 59),
        ("hour", dt.hour, 0, 23),
        ("day", dt.day, 1, 31),
        ("month", dt.month, 1, 12),
        ("weekday", dt.weekday(), 0, 6),  # Monday=0
    ]

    for field_name, current_val, min_val, max_val in checks:
        pattern = parsed[field_name]
        if pattern == "*":
            continue
        if pattern.startswith("*/"):
            step = int(pattern[2:])
            if current_val % step != 0:
                return False
            continue
        if "-" in pattern:
            lo, hi = pattern.split("-", 1)
            if not (int(lo) <= current_val <= int(hi)):
                return False
            continue
        if "," in pattern:
            vals = [int(v) for v in pattern.split(",")]
            if current_val not in vals:
                return False
            continue
        if current_val != int(pattern):
            return False

    return True


class Scheduler:
    """Background scheduler that checks for due tasks every 60 seconds."""

    def __init__(self, db: PostgresDB, redis: RedisClient, task_queue: TaskQueue) -> None:
        self.db = db
        self.redis = redis
        self.task_queue = task_queue
        self._running = False

    async def start(self) -> None:
        """Start the scheduler loop."""
        self._running = True
        logger.info("Scheduler started")
        while self._running:
            try:
                await self._check_scheduled_tasks()
            except Exception:
                logger.exception("Scheduler error")
            await asyncio.sleep(60)  # Check every minute

    async def stop(self) -> None:
        self._running = False

    async def _check_scheduled_tasks(self) -> None:
        """Check for tasks that are due to run."""
        now = datetime.now(timezone.utc)

        try:
            scheduled = await self.db.get_scheduled_tasks(status="active")
        except Exception:
            return

        for sched in scheduled:
            try:
                should_run = False
                trigger = sched.get("trigger_type", "cron")
                cron_expr = sched.get("cron_expression", "")
                next_run = sched.get("next_run_at")

                if trigger == "once":
                    # One-shot: run if next_run_at is past
                    if next_run and now >= next_run:
                        should_run = True
                elif trigger == "cron" and cron_expr:
                    # Cron: check if current minute matches
                    if _cron_matches(cron_expr, now):
                        # Avoid double-firing: check last_run
                        last_run = sched.get("last_run_at")
                        if not last_run or (now - last_run).total_seconds() > 55:
                            should_run = True

                if should_run:
                    await self._fire_scheduled_task(sched, now)

            except Exception:
                logger.exception(f"Error checking scheduled task {sched.get('id')}")

    async def _fire_scheduled_task(self, sched: dict, now: datetime) -> None:
        """Fire a scheduled task — create a project run or inject a task."""
        sched_id = sched["id"]
        project_id = sched.get("project_id")
        workflow = sched.get("workflow", {})

        if isinstance(workflow, str):
            import json
            try:
                workflow = json.loads(workflow)
            except Exception:
                workflow = {}

        task_type_str = workflow.get("task_type", "research")
        try:
            task_type = TaskType(task_type_str)
        except ValueError:
            task_type = TaskType.RESEARCH

        payload = workflow.get("payload", {})
        payload["scheduled_task_id"] = sched_id
        payload["scheduled_at"] = now.isoformat()

        # If no project_id, create a new project for this run
        if not project_id:
            from swarm.models.project import Project
            project = Project(
                name=sched.get("name", "Scheduled Task"),
                brief=workflow.get("brief", "Scheduled task execution"),
            )
            await self.db.create_project(project.to_dict())
            project_id = project.id

        task = Task(
            type=task_type,
            payload=payload,
            priority=TaskPriority(workflow.get("priority", 2)),
            project_id=project_id,
        )
        await self.task_queue.submit(task)

        # Update schedule
        run_count = sched.get("run_count", 0) + 1
        update_fields: dict[str, Any] = {
            "last_run_at": now,
            "run_count": run_count,
        }

        # One-shot tasks become inactive after running
        if sched.get("trigger_type") == "once":
            update_fields["status"] = "completed"

        # Check max_runs limit
        max_runs = sched.get("max_runs")
        if max_runs and run_count >= max_runs:
            update_fields["status"] = "completed"

        await self.db.update_scheduled_task(sched_id, **update_fields)

        logger.info(
            f"Scheduled task fired: {sched.get('name')} (id={sched_id}, "
            f"run #{run_count}, project={project_id})"
        )

        await self.redis.publish_event({
            "type": "scheduled_task_fired",
            "schedule_id": sched_id,
            "project_id": project_id,
            "task_id": task.id,
            "name": sched.get("name", ""),
        })
