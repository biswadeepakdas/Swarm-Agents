"""
Redis client — streams, pub/sub, and key-value for agent working memory.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

from swarm.config import config

logger = logging.getLogger("swarm.db.redis_client")


class RedisClient:
    def __init__(self) -> None:
        self.client: redis.Redis | None = None
        self.pubsub: redis.client.PubSub | None = None

    async def connect(self) -> None:
        self.client = redis.from_url(config.redis_url, decode_responses=True)
        await self.client.ping()
        logger.info("Redis connected")

    async def close(self) -> None:
        if self.pubsub:
            await self.pubsub.close()
        if self.client:
            await self.client.close()
            logger.info("Redis closed")

    # ── Stream operations (Task Queue) ────────────────────────────

    async def ensure_consumer_group(self) -> None:
        try:
            await self.client.xgroup_create(
                config.task_stream, config.task_group, id="0", mkstream=True
            )
            logger.info(f"Consumer group '{config.task_group}' created")
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def submit_task(self, task_data: dict[str, Any]) -> str:
        payload = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in task_data.items()}
        msg_id = await self.client.xadd(config.task_stream, payload)
        logger.debug(f"Task submitted to stream: {task_data.get('id')} (msg={msg_id})")
        return msg_id

    async def read_tasks(self, consumer_name: str, count: int = 1, block: int = 5000) -> list[tuple[str, dict]]:
        results = await self.client.xreadgroup(
            config.task_group,
            consumer_name,
            {config.task_stream: ">"},
            count=count,
            block=block,
        )
        if not results:
            return []
        tasks = []
        for _stream, messages in results:
            for msg_id, data in messages:
                parsed = {}
                for k, v in data.items():
                    try:
                        parsed[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        parsed[k] = v
                tasks.append((msg_id, parsed))
        return tasks

    async def ack_task(self, msg_id: str) -> None:
        await self.client.xack(config.task_stream, config.task_group, msg_id)

    async def submit_dead_letter(self, task_data: dict[str, Any]) -> None:
        payload = {k: json.dumps(v) if isinstance(v, (dict, list)) else str(v) for k, v in task_data.items()}
        await self.client.xadd(config.dead_letter_stream, payload)
        logger.warning(f"Task moved to dead letter queue: {task_data.get('id')}")

    # ── Pub/Sub (real-time events) ────────────────────────────────

    async def publish_event(self, event: dict[str, Any]) -> None:
        await self.client.publish(config.pubsub_channel, json.dumps(event))

    async def subscribe_events(self) -> redis.client.PubSub:
        self.pubsub = self.client.pubsub()
        await self.pubsub.subscribe(config.pubsub_channel)
        return self.pubsub

    # ── Key-Value (Agent short-term memory) ───────────────────────

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        data = json.dumps(value)
        if ttl:
            await self.client.setex(key, ttl, data)
        else:
            await self.client.set(key, data)

    async def get_json(self, key: str) -> Any | None:
        data = await self.client.get(key)
        return json.loads(data) if data else None

    async def delete_key(self, key: str) -> None:
        await self.client.delete(key)

    async def lpush_json(self, key: str, value: Any, max_len: int = 20) -> None:
        await self.client.lpush(key, json.dumps(value))
        await self.client.ltrim(key, 0, max_len - 1)

    async def lrange_json(self, key: str, start: int = 0, end: int = -1) -> list[Any]:
        items = await self.client.lrange(key, start, end)
        return [json.loads(item) for item in items]
