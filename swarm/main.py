"""
Swarm Agents — Main entry point.

Starts the FastAPI server, connects to databases,
runs migrations, and launches the task spawn loop.

Auto-detects available infrastructure:
  - PostgreSQL available → uses PostgresDB
  - PostgreSQL missing → falls back to in-memory MemoryDB
  - Redis available → uses Redis for streams + pub/sub
  - Redis missing → won't start (Redis is required)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from swarm.api.routes import init_routes, router
from swarm.api.websocket import init_websocket, ws_router
from swarm.config import config
from swarm.core.environment import Environment
from swarm.core.task_queue import TaskQueue
from swarm.db.redis_client import RedisClient

# ── Logging ───────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("swarm.main")

# ── Globals ───────────────────────────────────────────────────

db = None  # Will be set in lifespan
redis_client = RedisClient()
task_queue: TaskQueue | None = None
environment: Environment | None = None
spawn_loop_task: asyncio.Task | None = None


async def _init_db():
    """Try PostgreSQL first, fall back to in-memory DB."""
    global db

    # Try Postgres
    try:
        from swarm.db.postgres import PostgresDB
        pg = PostgresDB()
        await pg.connect()
        await pg.run_migrations()
        db = pg
        logger.info("Database: PostgreSQL (production mode)")
        return
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}). Falling back to in-memory DB.")

    # Fallback to memory
    from swarm.db.memory_db import MemoryDB
    mem = MemoryDB()
    await mem.connect()
    db = mem
    logger.info("Database: In-memory (testing mode — data lost on restart)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, task_queue, environment, spawn_loop_task

    logger.info("=" * 60)
    logger.info("  SWARM AGENTS ENGINE — Starting up")
    logger.info("=" * 60)

    # 1. Connect databases
    await _init_db()
    await redis_client.connect()

    # 1b. Clean up zombie agents/tasks from previous unclean shutdown
    cleanup = await db.cleanup_stale_on_startup()
    if cleanup["zombie_agents"] or cleanup["zombie_tasks"]:
        logger.warning(f"Cleaned up {cleanup['zombie_agents']} zombie agents, {cleanup['zombie_tasks']} zombie tasks")

    # 2. Initialize core components
    environment = Environment(db=db, redis=redis_client)
    task_queue = TaskQueue(redis=redis_client, db=db)

    # Wire them together (bidirectional)
    task_queue.set_environment(environment)
    environment.set_task_queue(task_queue)

    # 3. Initialize API routes
    init_routes(db, redis_client, task_queue, environment)
    init_websocket(redis_client)

    # 4. Start the spawn loop in the background
    spawn_loop_task = asyncio.create_task(task_queue.start_spawn_loop())
    logger.info("Spawn loop launched in background")

    logger.info("=" * 60)
    logger.info("  SWARM AGENTS ENGINE — Ready")
    logger.info(f"  Dashboard: http://localhost:{os.getenv('PORT', '8000')}")
    logger.info(f"  API docs:  http://localhost:{os.getenv('PORT', '8000')}/docs")
    logger.info(f"  Max concurrency: {config.max_concurrency}")
    logger.info(f"  LLM: {config.llm_model}")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down swarm engine...")
    if task_queue:
        await task_queue.stop()
    if spawn_loop_task:
        spawn_loop_task.cancel()
        try:
            await spawn_loop_task
        except asyncio.CancelledError:
            pass
    await redis_client.close()
    if db:
        await db.close()
    logger.info("Swarm engine stopped.")


# ── FastAPI App ───────────────────────────────────────────────

app = FastAPI(
    title="Swarm Agents Engine",
    description="Self-organizing multi-agent swarm that builds software products autonomously.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(ws_router)

# Serve static dashboard
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_dashboard():
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    is_dev = os.getenv("RAILWAY_ENVIRONMENT") is None
    uvicorn.run(
        "swarm.main:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,
        log_level="info",
    )
