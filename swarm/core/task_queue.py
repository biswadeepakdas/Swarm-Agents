"""
Component 1: Task Queue with Agent Spawning

The nervous system of the swarm. Every task that enters the Redis Stream
triggers the CREATION of a new agent. No pre-defined agent pool.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from swarm.config import config
from swarm.models.task import Task, TaskPriority, TaskStatus, TaskType

if TYPE_CHECKING:
    from swarm.core.environment import Environment
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.task_queue")


class TaskQueue:
    def __init__(self, redis: RedisClient, db: PostgresDB) -> None:
        self.redis = redis
        self.db = db
        self.environment: Environment | None = None
        self._running = False
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._consumer_name = f"worker-{uuid.uuid4().hex[:8]}"
        self._active_agents: dict[str, asyncio.Task] = {}
        self._recovered_tasks: set[str] = set()  # Track tasks we've already re-submitted

    def set_environment(self, env: Environment) -> None:
        self.environment = env

    # ── Submit tasks ──────────────────────────────────────────────

    async def submit(self, task: Task) -> str:
        """Submit a single task to the queue."""
        await self.db.create_task(task.to_dict())
        msg_id = await self.redis.submit_task(task.to_dict())

        await self.redis.publish_event({
            "type": "task_submitted",
            "task_id": task.id,
            "task_type": task.type.value,
            "project_id": task.project_id,
            "priority": task.priority.value,
        })

        logger.info(f"Task submitted: {task.id} ({task.type.value}) priority={task.priority.name}")
        return msg_id

    async def submit_batch(self, tasks: list[Task]) -> list[str]:
        """Submit multiple tasks at once (e.g., initial brief decomposition)."""
        sorted_tasks = sorted(tasks, key=lambda t: t.priority, reverse=True)
        msg_ids = []
        for task in sorted_tasks:
            msg_id = await self.submit(task)
            msg_ids.append(msg_id)
        logger.info(f"Batch submitted: {len(tasks)} tasks")
        return msg_ids

    # ── Spawn loop — THE core pattern ─────────────────────────────

    async def start_spawn_loop(self) -> None:
        """
        The main loop: read tasks from Redis Stream → spawn an agent for each.
        This is the heartbeat of the swarm.
        Also runs a watchdog every 30s to kill zombie agents and auto-complete projects.
        """
        self._running = True
        await self.redis.ensure_consumer_group()
        # Claim any orphaned pending messages from dead workers
        claimed = await self.redis.claim_pending_tasks(self._consumer_name)
        if claimed:
            logger.warning(f"Claimed {claimed} orphaned tasks from previous workers")
        logger.info(f"Spawn loop started (consumer={self._consumer_name}, max_concurrency={config.max_concurrency})")

        import time as _time
        last_watchdog = _time.time()

        while self._running:
            try:
                messages = await self.redis.read_tasks(self._consumer_name, count=1, block=500)
                if not messages:
                    # Clean up finished agent tasks
                    self._cleanup_finished()

                    # Watchdog: every 30s, kill zombie agents + auto-complete projects
                    if _time.time() - last_watchdog > 30:
                        last_watchdog = _time.time()
                        await self._watchdog()

                    continue

                for msg_id, task_data in messages:
                    await self._semaphore.acquire()
                    agent_task = asyncio.create_task(
                        self._process_task(msg_id, task_data)
                    )
                    self._active_agents[task_data.get("id", msg_id)] = agent_task
                    agent_task.add_done_callback(
                        lambda t, tid=task_data.get("id", msg_id): self._on_agent_done(tid, t)
                    )

            except asyncio.CancelledError:
                logger.info("Spawn loop cancelled")
                break
            except Exception:
                logger.exception("Error in spawn loop")
                await asyncio.sleep(1)

    async def _watchdog(self) -> None:
        """Periodic watchdog: kill stuck agents, recover orphaned tasks, auto-complete projects."""
        try:
            # Kill zombie agents (alive/working for more than timeout + 30s buffer)
            result = await self.db.cleanup_stale_on_startup()
            if result["zombie_agents"] or result["zombie_tasks"]:
                logger.warning(f"Watchdog cleanup: {result}")

            # Recover orphaned pending tasks (in DB but lost from Redis)
            await self._recover_orphaned_tasks()

            # Auto-complete projects where all tasks are done
            await self._check_project_completion()
        except Exception:
            logger.exception("Watchdog error")

    async def _recover_orphaned_tasks(self) -> None:
        """Re-submit pending tasks that have been stuck for >60s (lost Redis messages)."""
        from datetime import datetime as dt, timezone as tz
        try:
            projects = await self.db.get_projects() if hasattr(self.db, 'get_projects') else []
            for p in projects:
                if p.get("status") != "active":
                    continue
                pid = str(p["id"])
                tasks = await self.db.get_tasks(pid, status="pending")
                for t in tasks:
                    created = t.get("created_at")
                    if not created:
                        continue
                    if isinstance(created, str):
                        try:
                            created = dt.fromisoformat(created.replace("Z", "+00:00"))
                        except Exception:
                            continue
                    age = (dt.now(tz.utc) - created).total_seconds()
                    tid = t["id"]
                    if age > 60 and tid not in self._recovered_tasks and tid not in self._active_agents:
                        # Also release the Redis lock so the task can be picked up again
                        lock_key = f"swarm:task_lock:{tid}"
                        await self.redis.client.delete(lock_key)
                        task_obj = Task.from_dict(t)
                        await self.redis.submit_task(task_obj.to_dict())
                        self._recovered_tasks.add(tid)
                        logger.warning(f"Watchdog: re-submitted orphaned task {tid} (pending {age:.0f}s)")
        except Exception:
            logger.exception("Orphaned task recovery error")

    async def _check_project_completion(self) -> None:
        """If all tasks in a project are completed/dead, mark project as completed.
        Spawns EvalAgent and AssemblerAgent before finalizing."""
        try:
            projects = await self.db.get_projects() if hasattr(self.db, 'get_projects') else []
            for p in projects:
                if p.get("status") != "active":
                    continue
                pid = str(p["id"])
                tasks = await self.db.get_tasks(pid)
                if not tasks:
                    continue

                # Check if all tasks are terminal (completed, dead)
                all_done = all(t.get("status") in ("completed", "dead") for t in tasks)
                if not all_done:
                    continue

                # Check if we already have eval/assemble tasks (don't double-spawn)
                task_types = {t.get("type") for t in tasks}
                has_eval = "evaluate_project" in task_types
                has_assemble = "assemble_deliverables" in task_types

                # Only mark complete if eval/assemble are done (or we need to spawn them)
                if not has_eval and len(tasks) >= 3:
                    # Spawn evaluation task
                    eval_task = Task(
                        type=TaskType.EVALUATE_PROJECT,
                        payload={
                            "trigger": "project_completion",
                            "project_name": p.get("name", ""),
                            "brief": p.get("brief", ""),
                        },
                        priority=TaskPriority.HIGH,
                        project_id=pid,
                    )
                    await self.submit(eval_task)
                    logger.info(f"Project {pid}: spawned EvalAgent for quality check")
                    continue

                if not has_assemble and has_eval:
                    # Spawn deliverables assembly task
                    assemble_task = Task(
                        type=TaskType.ASSEMBLE_DELIVERABLES,
                        payload={
                            "trigger": "project_completion",
                            "project_name": p.get("name", ""),
                            "brief": p.get("brief", ""),
                        },
                        priority=TaskPriority.NORMAL,
                        project_id=pid,
                    )
                    await self.submit(assemble_task)
                    logger.info(f"Project {pid}: spawned IntegrationAgent for assembly")
                    continue

                # Generate project summary
                summary = await self._generate_project_summary(p, tasks)

                from datetime import datetime as _dt, timezone as _tz
                await self.db.update_project(
                    pid,
                    status="completed",
                    summary=summary,
                    completed_at=_dt.now(_tz.utc),
                )
                await self.redis.publish_event({
                    "type": "project_completed",
                    "project_id": str(pid),
                    "summary": summary[:500],
                })
                logger.info(f"Project {pid} auto-completed (all {len(tasks)} tasks done)")

        except Exception:
            logger.exception("Project completion check error")

    async def _generate_project_summary(self, project: dict, tasks: list[dict]) -> str:
        """Generate a concise project summary."""
        try:
            artifacts = await self.db.query_artifacts(str(project["id"]))
            art_types = {}
            for a in artifacts:
                t = a.get("type", "unknown")
                art_types[t] = art_types.get(t, 0) + 1

            completed = sum(1 for t in tasks if t.get("status") == "completed")
            failed = sum(1 for t in tasks if t.get("status") in ("failed", "dead"))

            summary = f"## Project: {project.get('name', 'Untitled')}\n\n"
            summary += f"**Brief:** {project.get('brief', '')[:200]}\n\n"
            summary += f"**Results:** {completed} tasks completed, {failed} failed, {len(artifacts)} artifacts produced\n\n"
            summary += "**Artifacts:**\n"
            for atype, count in sorted(art_types.items()):
                summary += f"- {atype}: {count}\n"
            return summary
        except Exception:
            return f"Project {project.get('name', '')} completed."

    def _on_agent_done(self, task_id: str, future: asyncio.Task) -> None:
        self._semaphore.release()
        self._active_agents.pop(task_id, None)
        if future.exception():
            logger.error(f"Agent for task {task_id} failed: {future.exception()}")

    def _cleanup_finished(self) -> None:
        finished = [tid for tid, t in self._active_agents.items() if t.done()]
        for tid in finished:
            self._active_agents.pop(tid, None)

    async def _process_task(self, msg_id: str, task_data: dict[str, Any]) -> None:
        """Spawn a new agent and let it handle the task."""
        from swarm.core.agent import SwarmAgent

        task = Task.from_dict(task_data)

        # Deduplication: skip if task is already completed/active/dead
        try:
            existing = await self.db.get_tasks(task.project_id)
            for t in existing:
                if t["id"] == task.id and t.get("status") in ("completed", "active", "dead"):
                    logger.info(f"Skipping duplicate task {task.id} (already {t['status']})")
                    await self.redis.ack_task(msg_id)
                    return
        except Exception:
            pass  # If check fails, proceed anyway

        # Atomic lock: use Redis SETNX to prevent duplicate spawns for same task
        lock_key = f"swarm:task_lock:{task.id}"
        got_lock = await self.redis.client.set(lock_key, self._consumer_name, nx=True, ex=300)
        if not got_lock:
            logger.info(f"Skipping duplicate task {task.id} (locked by another worker)")
            await self.redis.ack_task(msg_id)
            return

        task.status = TaskStatus.ACTIVE
        task.started_at = datetime.now(timezone.utc)

        try:
            await self.db.update_task(task.id, status="active", started_at=task.started_at)

            # Emit progress so the user knows something is happening
            await self.redis.publish_event({
                "type": "agent_progress",
                "agent_id": "",
                "agent_name": "Spawn Loop",
                "project_id": task.project_id,
                "task_type": task.type.value,
                "phase": "task_picked_up",
                "detail": f"Processing {task.type.value} task (id={task.id[:8]}...)",
            })

            # Check if environment has unmet dependencies
            if self.environment and task.dependencies:
                unmet = await self.environment.check_blockers(task)
                if unmet:
                    logger.info(f"Task {task.id} blocked on: {unmet}. Setting to WAITING.")
                    task.status = TaskStatus.WAITING
                    await self.db.update_task(task.id, status="waiting")
                    await self.redis.ack_task(msg_id)
                    return

            # SPAWN a new agent
            agent = SwarmAgent(
                task=task,
                task_queue=self,
                environment=self.environment,
                db=self.db,
                redis=self.redis,
            )

            # Link the agent to the task so the graph can draw edges
            await self.db.update_task(
                task.id,
                assigned_agent_id=agent.identity.id,
            )

            await self.redis.publish_event({
                "type": "agent_spawned",
                "agent_id": agent.identity.id,
                "agent_name": agent.identity.name,
                "task_id": task.id,
                "task_type": task.type.value,
                "project_id": task.project_id,
            })

            # Agent executes autonomously
            result = await asyncio.wait_for(
                agent.execute(),
                timeout=config.agent_timeout_seconds,
            )

            # Success — ack the message, update task
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now(timezone.utc)
            task.result = result
            await self.db.update_task(
                task.id,
                status="completed",
                completed_at=task.completed_at,
                result=result or {},
            )
            await self.redis.ack_task(msg_id)

            await self.redis.publish_event({
                "type": "task_completed",
                "task_id": task.id,
                "agent_id": agent.identity.id,
                "project_id": task.project_id,
            })

            logger.info(f"Task {task.id} completed by {agent.identity.name}")

        except asyncio.TimeoutError:
            logger.error(f"Task {task.id} timed out after {config.agent_timeout_seconds}s")
            await self._handle_failure(task, msg_id, "Agent timed out")

        except Exception as e:
            logger.exception(f"Task {task.id} failed: {e}")
            await self._handle_failure(task, msg_id, str(e))

    async def _handle_failure(self, task: Task, msg_id: str, error: str) -> None:
        # Release the task lock so retries can pick it up
        lock_key = f"swarm:task_lock:{task.id}"
        try:
            await self.redis.client.delete(lock_key)
        except Exception:
            pass
        task.retry_count += 1
        if task.retry_count >= config.task_retry_limit:
            task.status = TaskStatus.DEAD
            await self.db.update_task(task.id, status="dead", error=error, retry_count=task.retry_count)
            await self.redis.submit_dead_letter(task.to_dict())
            await self.redis.ack_task(msg_id)
            logger.error(f"Task {task.id} moved to dead letter queue after {task.retry_count} retries")
        else:
            task.status = TaskStatus.PENDING
            await self.db.update_task(task.id, status="pending", error=error, retry_count=task.retry_count)
            await self.redis.submit_task(task.to_dict())
            await self.redis.ack_task(msg_id)
            logger.warning(f"Task {task.id} re-queued (retry {task.retry_count}/{config.task_retry_limit})")

        await self.redis.publish_event({
            "type": "task_failed",
            "task_id": task.id,
            "error": error,
            "retry_count": task.retry_count,
            "project_id": task.project_id,
        })

    async def stop(self) -> None:
        self._running = False
        for tid, agent_task in self._active_agents.items():
            agent_task.cancel()
        logger.info("Spawn loop stopped")

    @property
    def active_count(self) -> int:
        return len(self._active_agents)
