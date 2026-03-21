"""
The Swarm Agent — an autonomous entity.

Lifecycle: born → load context → plan → query environment → execute → publish → die.
Each agent gets its own identity, memory, persona, and LLM backbone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import litellm

from swarm.config import config
from swarm.core.agent_memory import AgentMemory
from swarm.core.interaction import InteractionProtocol
from swarm.models.agent import AgentIdentity, AgentStatus
from swarm.models.artifact import Artifact, ArtifactType
from swarm.models.task import Task, TaskType

if TYPE_CHECKING:
    from swarm.core.environment import Environment
    from swarm.core.task_queue import TaskQueue
    from swarm.db.postgres import PostgresDB
    from swarm.db.redis_client import RedisClient

logger = logging.getLogger("swarm.agent")

# Map task type → artifact type the agent should produce
TASK_OUTPUT_MAP: dict[TaskType, ArtifactType] = {
    TaskType.ANALYZE_REQUIREMENTS: ArtifactType.REQUIREMENTS_DOC,
    TaskType.PLAN_ARCHITECTURE: ArtifactType.ARCHITECTURE_PLAN,
    TaskType.DESIGN_DATABASE: ArtifactType.DATABASE_SCHEMA,
    TaskType.DESIGN_UI: ArtifactType.UI_DESIGN,
    TaskType.CREATE_API: ArtifactType.CODE_FILE,
    TaskType.WRITE_CODE: ArtifactType.CODE_FILE,
    TaskType.BUILD_FRONTEND_COMPONENT: ArtifactType.FRONTEND_COMPONENT,
    TaskType.REVIEW_CODE: ArtifactType.REVIEW,
    TaskType.WRITE_TESTS: ArtifactType.TEST_SUITE,
    TaskType.WRITE_DOCS: ArtifactType.DOCUMENTATION,
    TaskType.RESEARCH: ArtifactType.DOCUMENTATION,
    TaskType.DEBUG: ArtifactType.BUG_REPORT,
    TaskType.DEPLOY: ArtifactType.DEPLOYMENT_CONFIG,
    TaskType.FIX_CODE: ArtifactType.CODE_FILE,
    TaskType.INTEGRATION_TEST: ArtifactType.TEST_SUITE,
    TaskType.RESOLVE_CONFLICT: ArtifactType.DECISION,
}


class SwarmAgent:
    """
    An autonomous agent spawned for a single task.
    Born, executes, publishes results, optionally spawns sub-tasks, then dies.
    """

    def __init__(
        self,
        task: Task,
        task_queue: TaskQueue,
        environment: Environment,
        db: PostgresDB,
        redis: RedisClient,
    ) -> None:
        self.task = task
        self.task_queue = task_queue
        self.environment = environment
        self.db = db
        self.redis = redis

        # Create identity from task type
        self.identity = AgentIdentity(
            task_type=task.type,
            project_id=task.project_id,
            task_id=task.id,
        )

        # Per-agent memory
        self.memory = AgentMemory(
            agent_id=self.identity.id,
            project_id=task.project_id,
            redis=redis,
            db=db,
        )

        # Interaction protocol
        self.interaction = InteractionProtocol(environment, task_queue)

        # LLM config
        self._model = config.llm_model
        self._max_tokens = config.llm_max_tokens_per_agent

    async def _emit_progress(self, phase: str, detail: str = "") -> None:
        """Emit a real-time progress event so the user sees what's happening."""
        try:
            await self.redis.publish_event({
                "type": "agent_progress",
                "agent_id": self.identity.id,
                "agent_name": self.identity.name,
                "project_id": self.task.project_id,
                "task_type": self.task.type.value,
                "phase": phase,
                "detail": detail,
            })
        except Exception:
            pass  # Never let progress events break the agent

    async def execute(self) -> dict[str, Any]:
        """
        Main agent lifecycle:
        1. Register self in DB
        2. Load context from environment + memory
        3. Discover relevant artifacts
        4. Call LLM to produce output
        5. Publish artifact to environment
        6. Update memory
        7. Die
        """
        import time as _time

        logger.info(f"Agent {self.identity.name} BORN for task {self.task.id} ({self.task.type.value})")

        # 1. Register
        await self._register()
        await self._emit_progress("registered", "Agent online, loading context...")

        try:
            # 2. Load project context
            env_context = await self.environment.get_project_state(self.task.project_id)
            await self._emit_progress("context_loaded", f"Loaded project state ({len(env_context)} chars)")

            # 3. Discover relevant artifacts
            relevant_artifacts = await self.interaction.discover_relevant_artifacts(
                self.task.project_id, self.task
            )
            art_count = len(relevant_artifacts) if relevant_artifacts else 0
            await self._emit_progress("artifacts_discovered", f"Found {art_count} relevant artifact(s)")

            # 4. Build prompt and call LLM
            system_prompt = await self._build_system_prompt(env_context, relevant_artifacts)
            user_prompt = self._build_user_prompt(relevant_artifacts)

            await self.memory.append_reasoning({
                "action": "calling_llm",
                "result": f"Sending prompt ({len(system_prompt)} chars system, {len(user_prompt)} chars user)",
            })

            await self._emit_progress("calling_llm", f"Sending {len(system_prompt) + len(user_prompt)} chars to {self._model}...")

            t0 = _time.time()
            llm_response = await self._call_llm(system_prompt, user_prompt)
            elapsed = _time.time() - t0

            await self._emit_progress("llm_responded", f"LLM responded in {elapsed:.1f}s ({len(llm_response)} chars)")

            # 5. Parse and publish artifact
            artifact = self._parse_output(llm_response)
            await self._emit_progress("publishing", f"Publishing {artifact.type.value}: {artifact.name}")

            await self.environment.publish_artifact(artifact)

            await self.memory.append_reasoning({
                "action": "published_artifact",
                "result": f"Published {artifact.type.value}: {artifact.name}",
            })

            # 6. Store long-term memory
            memory_summary = (
                f"Completed {self.task.type.value} task. "
                f"Produced {artifact.type.value}: {artifact.name}. "
                f"Tags: {artifact.tags}"
            )
            await self.memory.memorize(memory_summary, tags=artifact.tags)

            # 7. Die
            await self._die()
            await self._emit_progress("completed", f"Done in {_time.time() - t0:.1f}s total")

            return {
                "artifact_id": artifact.id,
                "artifact_type": artifact.type.value,
                "artifact_name": artifact.name,
            }

        except Exception as e:
            logger.exception(f"Agent {self.identity.name} failed: {e}")
            await self._emit_progress("failed", str(e)[:200])
            await self._die(error=str(e))
            raise

    # ── Prompt building ───────────────────────────────────────────

    async def _build_system_prompt(
        self, env_context: dict, relevant_artifacts: list[dict]
    ) -> str:
        """Build the system prompt with persona + context + memory."""
        from swarm.personas import get_persona_prompt

        # Base persona prompt
        persona_prompt = get_persona_prompt(self.identity.role)

        # Memory context
        memory_context = await self.memory.get_context_window(
            task_summary=f"{self.task.type.value}: {json.dumps(self.task.payload)}",
            env_context=env_context,
        )

        parts = [
            persona_prompt,
            "",
            "# Your Identity",
            f"Agent ID: {self.identity.id}",
            f"Role: {self.identity.persona}",
            f"Task: {self.task.type.value}",
            "",
            memory_context,
        ]

        return "\n".join(parts)

    def _build_user_prompt(self, relevant_artifacts: list[dict]) -> str:
        """Build the user prompt with task details + discovered artifacts."""
        parts = [
            f"# Task: {self.task.type.value}",
            f"Task ID: {self.task.id}",
            "",
        ]

        # Task payload
        payload = self.task.payload
        if payload:
            parts.append("## Task Details")
            for key, val in payload.items():
                parts.append(f"- **{key}**: {val}")
            parts.append("")

        # Relevant artifacts from environment
        if relevant_artifacts:
            parts.append("## Relevant Artifacts from Other Agents")
            for art in relevant_artifacts[:10]:  # cap at 10 to control tokens
                parts.append(f"### {art['name']} ({art['type']})")
                parts.append(f"Created by: {art.get('agent_id', 'unknown')}")
                parts.append(f"Tags: {art.get('tags', [])}")
                # Include content, but truncate for token budget
                content = art.get("content", "")
                if len(content) > 3000:
                    content = content[:3000] + "\n... [truncated]"
                parts.append(f"```\n{content}\n```")
                parts.append("")

        # Output instructions
        output_type = TASK_OUTPUT_MAP.get(self.task.type, ArtifactType.DOCUMENTATION)
        parts.append("## Output Requirements")
        parts.append(f"Produce a **{output_type.value}** artifact.")
        parts.append("Structure your output as follows:")
        parts.append("```")
        parts.append("ARTIFACT_NAME: <descriptive name>")
        parts.append("ARTIFACT_TAGS: <comma-separated tags>")
        parts.append("ARTIFACT_CONTENT:")
        parts.append("<your full output here>")
        parts.append("```")

        # Special instructions for architecture plans
        if self.task.type == TaskType.PLAN_ARCHITECTURE:
            parts.append("")
            parts.append("## Architecture Plan Special Instructions")
            parts.append("After your plan, include a COMPONENTS section as JSON:")
            parts.append("```json")
            parts.append("COMPONENTS: [")
            parts.append('  {"name": "component_name", "type": "task_type", "description": "...", "priority": 2, "dependencies": ["tag1"]},')
            parts.append("  ...")
            parts.append("]")
            parts.append("```")
            parts.append("Valid task types: create_api, design_database, build_frontend_component, write_docs, deploy")

        # Special instructions for reviews
        if self.task.type == TaskType.REVIEW_CODE:
            parts.append("")
            parts.append("## Review Special Instructions")
            parts.append("End your review with:")
            parts.append("REVIEW_VERDICT: PASS or FAIL")
            parts.append("ISSUES: [list of issues, or empty]")

        return "\n".join(parts)

    # ── LLM call ──────────────────────────────────────────────────

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """Call the LLM via litellm with explicit timeout. Supports Anthropic, NVIDIA NIM, OpenAI, etc."""
        import asyncio as _asyncio
        import os

        # Set provider-specific env vars for litellm auto-detection
        if config.llm_api_key:
            if "nvidia" in config.llm_provider.lower() or "nvidia" in self._model.lower():
                os.environ["NVIDIA_NIM_API_KEY"] = config.llm_api_key
            elif "anthropic" in config.llm_provider.lower():
                os.environ.setdefault("ANTHROPIC_API_KEY", config.llm_api_key)
            else:
                os.environ.setdefault("OPENAI_API_KEY", config.llm_api_key)

        llm_timeout = 90  # seconds — hard cap on any single LLM call

        try:
            logger.info(f"Agent {self.identity.id} calling LLM model={self._model} timeout={llm_timeout}s")

            response = await _asyncio.wait_for(
                litellm.acompletion(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=self._max_tokens,
                    temperature=0.7,
                    api_key=config.llm_api_key,
                    timeout=llm_timeout,  # litellm's own timeout
                ),
                timeout=llm_timeout + 10,  # asyncio hard timeout (slightly longer)
            )
            content = response.choices[0].message.content

            # Track token usage
            usage = response.usage
            if usage:
                logger.info(
                    f"Agent {self.identity.id} LLM usage: "
                    f"prompt={usage.prompt_tokens} completion={usage.completion_tokens}"
                )
                await self.redis.publish_event({
                    "type": "llm_usage",
                    "agent_id": self.identity.id,
                    "project_id": self.task.project_id,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                })

            return content

        except _asyncio.TimeoutError:
            err = f"LLM call timed out after {llm_timeout}s (model={self._model})"
            logger.error(f"Agent {self.identity.id}: {err}")
            await self._emit_progress("llm_timeout", err)
            raise TimeoutError(err)

        except Exception as e:
            logger.error(f"LLM call failed for agent {self.identity.id}: {e}")
            await self._emit_progress("llm_error", f"{type(e).__name__}: {str(e)[:150]}")
            raise

    # ── Output parsing ────────────────────────────────────────────

    def _parse_output(self, llm_response: str) -> Artifact:
        """Parse LLM response into an Artifact."""
        output_type = TASK_OUTPUT_MAP.get(self.task.type, ArtifactType.DOCUMENTATION)

        # Try to extract structured output
        name = self._extract_field(llm_response, "ARTIFACT_NAME") or f"{self.task.type.value}_{self.task.id[:8]}"
        tags_str = self._extract_field(llm_response, "ARTIFACT_TAGS") or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        content = self._extract_content(llm_response)

        # Ensure task-relevant tags
        tags.append(self.task.type.value)
        if self.task.payload.get("component"):
            tags.append(self.task.payload["component"].lower().replace(" ", "_"))
        tags = list(set(tags))

        # Build metadata
        metadata: dict[str, Any] = {}
        if self.task.type == TaskType.REVIEW_CODE:
            verdict = self._extract_field(llm_response, "REVIEW_VERDICT") or "PASS"
            issues_str = self._extract_field(llm_response, "ISSUES") or "[]"
            has_issues = "FAIL" in verdict.upper()
            metadata["verdict"] = verdict.strip()
            metadata["has_issues"] = has_issues
            metadata["issues"] = issues_str

        if self.task.type == TaskType.PLAN_ARCHITECTURE:
            components = self._extract_components(llm_response)
            if components:
                metadata["components"] = components

        return Artifact(
            project_id=self.task.project_id,
            task_id=self.task.id,
            agent_id=self.identity.id,
            type=output_type,
            name=name,
            content=content,
            tags=tags,
            metadata=metadata,
        )

    def _extract_field(self, text: str, field: str) -> str | None:
        for line in text.split("\n"):
            if line.strip().startswith(f"{field}:"):
                return line.split(":", 1)[1].strip()
        return None

    def _extract_content(self, text: str) -> str:
        marker = "ARTIFACT_CONTENT:"
        idx = text.find(marker)
        if idx >= 0:
            content = text[idx + len(marker):]
            # Strip leading/trailing ``` if present
            content = content.strip()
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            return content.strip()
        # Fallback: return the whole response
        return text.strip()

    def _extract_components(self, text: str) -> list[dict]:
        marker = "COMPONENTS:"
        idx = text.find(marker)
        if idx < 0:
            return []
        json_str = text[idx + len(marker):].strip()
        # Find the JSON array
        start = json_str.find("[")
        if start < 0:
            return []
        depth = 0
        end = start
        for i, ch in enumerate(json_str[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        try:
            return json.loads(json_str[start:end + 1])
        except json.JSONDecodeError:
            logger.warning("Failed to parse COMPONENTS JSON from architecture plan")
            return []

    # ── Lifecycle ─────────────────────────────────────────────────

    async def _register(self) -> None:
        """Register agent in DB."""
        await self.db.create_agent(self.identity.to_dict())
        self.identity.status = AgentStatus.WORKING
        await self.db.update_agent(self.identity.id, status="working")

    async def _die(self, error: str | None = None) -> None:
        """Agent death: clear short-term memory, update status."""
        self.identity.status = AgentStatus.DEAD
        self.identity.died_at = datetime.now(timezone.utc)
        await self.db.update_agent(
            self.identity.id,
            status="dead",
            died_at=self.identity.died_at,
        )
        await self.memory.forget_short_term()

        await self.redis.publish_event({
            "type": "agent_died",
            "agent_id": self.identity.id,
            "agent_name": self.identity.name,
            "project_id": self.task.project_id,
            "error": error,
        })

        logger.info(f"Agent {self.identity.name} DIED" + (f" (error: {error})" if error else ""))
