"""
WebSocket endpoint for real-time swarm activity feed.

WS /api/projects/{project_id}/stream
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.api.websocket")
ws_router = APIRouter()

_redis: RedisClient | None = None


def init_websocket(redis: RedisClient):
    global _redis
    _redis = redis


class ConnectionManager:
    """Manages WebSocket connections grouped by project."""

    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, project_id: str, ws: WebSocket):
        await ws.accept()
        if project_id not in self.connections:
            self.connections[project_id] = []
        self.connections[project_id].append(ws)
        logger.info(f"WebSocket connected: project {project_id}")

    def disconnect(self, project_id: str, ws: WebSocket):
        if project_id in self.connections:
            self.connections[project_id] = [
                c for c in self.connections[project_id] if c != ws
            ]
            if not self.connections[project_id]:
                del self.connections[project_id]

    async def broadcast(self, project_id: str, data: dict):
        conns = self.connections.get(project_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(project_id, ws)


manager = ConnectionManager()


@ws_router.websocket("/api/projects/{project_id}/stream")
async def swarm_stream(websocket: WebSocket, project_id: str):
    """Real-time feed of all swarm activity for a project."""
    await manager.connect(project_id, websocket)

    try:
        # Start a background task to relay Redis pub/sub events
        relay_task = asyncio.create_task(
            _relay_events(project_id, websocket)
        )

        # Keep connection alive by reading (client can send pings)
        while True:
            try:
                data = await websocket.receive_text()
                # Client can send "ping" to keep alive
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                break

    finally:
        relay_task.cancel()
        manager.disconnect(project_id, websocket)
        logger.info(f"WebSocket disconnected: project {project_id}")


async def _relay_events(project_id: str, ws: WebSocket):
    """Subscribe to Redis pub/sub and relay events matching this project."""
    if not _redis:
        return

    pubsub = await _redis.subscribe_events()
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    if data.get("project_id") == project_id:
                        await ws.send_json(data)
                except (json.JSONDecodeError, Exception):
                    pass
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.close()
