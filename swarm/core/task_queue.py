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
        """
        self._running = True
        await self.redis.ensure_consumer_group()
        logger.info(f"Spawn loop started (consumer={self._consumer_name}, max_concurrency={config.max_concurrency})")

        while self._running:
            try:
                messages = await self.redis.read_tasks(self._consumer_name, count=1, block=2000)
                if not messages:
                    # Clean up finished agent tasks
                    self._cleanup_finished()
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
        task.status = TaskStatus.ACTIVE
        task.started_at = datetime.now(timezone.utc)

        try:
            await self.db.update_task(task.id, status="active", started_at=task.started_at)

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
