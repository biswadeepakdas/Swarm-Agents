"""
The Swarm Agent — an autonomous, tool-using entity.

Lifecycle: born → load context → tool loop (think→act→observe) → publish → die.
Each agent gets its own identity, memory, persona, tools, and LLM backbone.

This is the Hive Computer-style agent: iterative tool use, multi-model
routing, code execution, web search, and structured file output.
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
from swarm.core.model_router import ModelConfig, get_router
from swarm.core.tool_registry import get_tools_for_task, get_tool_names_for_task
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
    # New: Hive Computer spec
    TaskType.EVALUATE_PROJECT: ArtifactType.EVALUATION_REPORT,
    TaskType.ASSEMBLE_DELIVERABLES: ArtifactType.DELIVERABLES_PACKAGE,
    TaskType.GENERATE_MEDIA: ArtifactType.MEDIA_ASSET,
    TaskType.COUNCIL_REVIEW: ArtifactType.DECISION,
}

# Maximum tool-use iterations per agent (keep low for fast completion)
MAX_TOOL_STEPS = 5


class SwarmAgent:
    """
    An autonomous agent spawned for a single task.
    Uses iterative tool calling: think → call tool → observe → think → ... → submit artifact.
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

        # Multi-model routing
        self._model_config: ModelConfig = get_router().select_model(task.type)
        self._model = self._model_config.model
        self._max_tokens = self._model_config.max_tokens

        # Tool state
        self._tool_names = get_tool_names_for_task(task.type)
        self._tool_schemas = get_tools_for_task(task.type)
        self._submitted_artifact: dict | None = None  # Set by submit_artifact tool
        self._files_written: list[str] = []  # Track files written via write_file tool

        # Initialize file ops for this project
        self._file_ops = None
        self._code_runner = None
        self._web_search = None
        self._web_browser = None
        self._env_query = None
        self._media_gen = None
        self._github_tool = None
        self._council = None
        self._ask_user_count = 0  # Limit ask_user calls per agent

    def _init_tools(self) -> None:
        """Lazy-initialize tool instances."""
        import tempfile
        from pathlib import Path

        if "write_file" in self._tool_names or "read_file" in self._tool_names:
            from swarm.tools.file_ops import FileOps
            project_dir = Path(tempfile.gettempdir()) / "swarm_output" / self.task.project_id
            self._file_ops = FileOps(project_dir)

        if "run_python" in self._tool_names:
            from swarm.tools.code_runner import CodeRunner
            self._code_runner = CodeRunner()

        if "web_search" in self._tool_names:
            from swarm.tools.web_search import WebSearchTool
            self._web_search = WebSearchTool()

        if "fetch_page" in self._tool_names:
            from swarm.tools.web_browser import WebBrowserTool
            self._web_browser = WebBrowserTool()

        if "query_artifacts" in self._tool_names:
            from swarm.tools.environment_query import EnvironmentQueryTool
            self._env_query = EnvironmentQueryTool(self.environment, self.task.project_id)

        if "generate_image" in self._tool_names:
            from swarm.tools.media_gen import MediaGenTool
            self._media_gen = MediaGenTool()

        if "github_push" in self._tool_names:
            from swarm.tools.github_tool import GitHubTool
            self._github_tool = GitHubTool()

        if "council_deliberate" in self._tool_names:
            from swarm.core.council import get_council
            self._council = get_council()

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
            pass

    async def execute(self) -> dict[str, Any]:
        """
        Main agent lifecycle:
        1. Register self in DB
        2. Load context from environment + memory
        3. Run tool-use loop (think → act → observe → ...)
        4. Publish artifact to environment
        5. Die
        """
        import time as _time

        logger.info(f"Agent {self.identity.name} BORN for task {self.task.id} ({self.task.type.value})")

        # 1. Register
        await self._register()
        await self._emit_progress("registered", "Agent online, loading context...")

        try:
            # Initialize tools
            self._init_tools()

            # 2. Load project context
            env_context = await self.environment.get_project_state(self.task.project_id)
            await self._emit_progress("context_loaded", f"Loaded project state ({len(env_context)} chars)")

            # 3. Discover relevant artifacts
            relevant_artifacts = await self.interaction.discover_relevant_artifacts(
                self.task.project_id, self.task
            )
            art_count = len(relevant_artifacts) if relevant_artifacts else 0
            await self._emit_progress("artifacts_discovered", f"Found {art_count} relevant artifact(s)")

            # 4. Build prompts
            system_prompt = await self._build_system_prompt(env_context, relevant_artifacts)
            user_prompt = self._build_user_prompt(relevant_artifacts)

            await self._emit_progress("calling_llm",
                f"Using {self._model_config.model} ({len(system_prompt) + len(user_prompt)} chars)")

            # 5. Run the tool-use loop
            t0 = _time.time()
            artifact = await self._tool_loop(system_prompt, user_prompt)
            elapsed = _time.time() - t0

            # 6. Publish artifact
            await self._emit_progress("publishing", f"Publishing {artifact.type.value}: {artifact.name}")
            await self.environment.publish_artifact(artifact)

            # 7. Store long-term memory
            memory_summary = (
                f"Completed {self.task.type.value} task. "
                f"Produced {artifact.type.value}: {artifact.name}. "
                f"Tags: {artifact.tags}"
            )
            if self._files_written:
                memory_summary += f" Files: {', '.join(self._files_written)}"
            await self.memory.memorize(memory_summary, tags=artifact.tags)

            # 8. Die
            await self._die()
            await self._emit_progress("completed", f"Done in {elapsed:.1f}s total")

            return {
                "artifact_id": artifact.id,
                "artifact_type": artifact.type.value,
                "artifact_name": artifact.name,
                "files_written": self._files_written,
                "model_used": self._model_config.model,
            }

        except Exception as e:
            logger.exception(f"Agent {self.identity.name} failed: {e}")
            await self._emit_progress("failed", str(e)[:200])
            await self._die(error=str(e))
            raise

    # ── Tool-Use Loop ──────────────────────────────────────────────

    async def _tool_loop(self, system_prompt: str, user_prompt: str) -> Artifact:
        """
        Iterative tool-use loop: call LLM → if tool_call, execute tool → feed result back → repeat.
        Stops when: agent calls submit_artifact, or max steps reached, or LLM returns no tool call.
        """
        import asyncio as _asyncio

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        for step in range(MAX_TOOL_STEPS):
            await self._emit_progress("tool_step", f"Step {step + 1}/{MAX_TOOL_STEPS}")

            # Call LLM with tool definitions
            response = await self._call_llm_with_tools(messages)
            message = response.choices[0].message

            # Track token usage
            usage = response.usage
            if usage:
                logger.info(
                    f"Agent {self.identity.id} LLM usage (step {step+1}): "
                    f"prompt={usage.prompt_tokens} completion={usage.completion_tokens}"
                )
                await self.redis.publish_event({
                    "type": "llm_usage",
                    "agent_id": self.identity.id,
                    "project_id": self.task.project_id,
                    "model": self._model_config.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                })

            # Check if the LLM wants to call tools
            tool_calls = getattr(message, 'tool_calls', None)

            if not tool_calls:
                # No tool call — LLM produced a final text response.
                # Fall back to legacy parsing (for models that don't do function calling well)
                content = message.content or ""
                if self._submitted_artifact:
                    return self._build_artifact_from_submission(self._submitted_artifact)
                return self._parse_output(content)

            # Append the assistant message (with tool calls) to conversation
            messages.append(self._serialize_assistant_message(message))

            # Execute each tool call
            for tool_call in tool_calls:
                fn_name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                await self._emit_progress("tool_call", f"Calling {fn_name}...")
                logger.info(f"Agent {self.identity.id} tool call: {fn_name}({list(args.keys())})")

                # Execute the tool
                result = await self._execute_tool(fn_name, args)

                # Append tool result to conversation
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result[:4000],  # Cap tool output to control context size
                })

                # If submit_artifact was called, we're done
                if fn_name == "submit_artifact" and self._submitted_artifact:
                    return self._build_artifact_from_submission(self._submitted_artifact)

        # Max steps reached — use whatever we have
        logger.warning(f"Agent {self.identity.id} hit max tool steps ({MAX_TOOL_STEPS})")
        if self._submitted_artifact:
            return self._build_artifact_from_submission(self._submitted_artifact)

        # Emergency: ask LLM for final output without tools
        messages.append({"role": "user", "content": "You've used all your tool steps. Please provide your final ARTIFACT_NAME, ARTIFACT_TAGS, and ARTIFACT_CONTENT now."})
        final_response = await self._call_llm_simple(messages)
        return self._parse_output(final_response)

    async def _execute_tool(self, name: str, args: dict) -> str:
        """Dispatch a tool call and return the result as a string."""
        try:
            if name == "run_python":
                if not self._code_runner:
                    return "Error: Code execution not available for this task type."
                result = await self._code_runner.run_python(args.get("code", ""))
                output = f"Exit code: {result.exit_code}\n"
                if result.stdout:
                    output += f"STDOUT:\n{result.stdout}\n"
                if result.stderr:
                    output += f"STDERR:\n{result.stderr}\n"
                if result.timed_out:
                    output += "TIMED OUT\n"
                return output

            elif name == "web_search":
                if not self._web_search:
                    return "Web search unavailable. Proceed with your existing knowledge and submit your artifact."
                # Limit to 2 searches per agent — no more research after that
                if not hasattr(self, '_search_count'):
                    self._search_count = 0
                self._search_count += 1
                if self._search_count > 2:
                    return "You've already searched twice. Stop researching and submit your artifact now using submit_artifact."
                results = await self._web_search.search(
                    args.get("query", ""),
                    max_results=args.get("max_results", 3),
                )
                if not results:
                    return "No results found. Proceed with your existing knowledge and submit your artifact."
                return "\n\n".join(
                    f"**{r.title}**\n{r.url}\n{r.snippet}" for r in results
                )

            elif name == "write_file":
                if not self._file_ops:
                    return "Error: File operations not available."
                path = args.get("path", "")
                content = args.get("content", "")
                written_path = await self._file_ops.write_file(path, content)
                self._files_written.append(path)
                return f"File written: {path} ({len(content)} bytes)"

            elif name == "read_file":
                if not self._file_ops:
                    return "Error: File operations not available."
                content = await self._file_ops.read_file(args.get("path", ""))
                return content[:4000]

            elif name == "list_files":
                if not self._file_ops:
                    return "Error: File operations not available."
                files = await self._file_ops.list_files(args.get("directory", "."))
                return "\n".join(files) if files else "(no files yet)"

            elif name == "query_artifacts":
                if not self._env_query:
                    return "Error: Environment query not available."
                art_type = args.get("artifact_type")
                tags = args.get("tags")
                if art_type:
                    results = await self._env_query.find_by_type(art_type)
                elif tags:
                    results = await self._env_query.find_by_tags(tags)
                else:
                    summary = await self._env_query.get_project_summary()
                    return json.dumps(summary, default=str)[:4000]
                # Return summaries
                return "\n\n".join(
                    f"[{r.get('type')}] {r.get('name')}\nTags: {r.get('tags', [])}\n{str(r.get('content', ''))[:1000]}"
                    for r in results[:5]
                )

            elif name == "fetch_page":
                if not self._web_browser:
                    return "Web browsing unavailable for this task type."
                url = args.get("url", "")
                if not url:
                    return "Error: URL is required."
                page = await self._web_browser.fetch_page(url)
                if page.success:
                    return f"**{page.title}**\nURL: {page.url}\n\n{page.text}"
                return f"Failed to fetch page: {page.error}. Proceed with existing knowledge."

            elif name == "ask_user":
                # Limit to 1 ask_user per agent to prevent infinite loops
                self._ask_user_count += 1
                if self._ask_user_count > 1:
                    return "You've already asked the user once. Proceed with your best judgment and submit your artifact."

                question = args.get("question", "")
                options = args.get("options", [])
                context = args.get("context", "")

                if not question:
                    return "Error: question is required."

                # Create interaction record in DB
                import uuid
                interaction_id = str(uuid.uuid4())
                try:
                    await self.db.create_interaction({
                        "id": interaction_id,
                        "project_id": self.task.project_id,
                        "task_id": self.task.id,
                        "agent_id": self.identity.id,
                        "question": question,
                        "options": options if isinstance(options, list) else [],
                        "context": context,
                        "status": "pending",
                    })
                except Exception as e:
                    logger.warning(f"Failed to create interaction: {e}")
                    return "Could not ask user (interaction system unavailable). Proceed with your best judgment."

                # Emit event so frontend shows the question
                await self.redis.publish_event({
                    "type": "agent_question",
                    "interaction_id": interaction_id,
                    "agent_id": self.identity.id,
                    "agent_name": self.identity.name,
                    "project_id": self.task.project_id,
                    "task_id": self.task.id,
                    "question": question,
                    "options": options,
                    "context": context,
                })

                await self._emit_progress("waiting_for_user", f"Asked: {question[:80]}")

                # Poll for user response (with timeout)
                import asyncio as _poll_asyncio
                poll_timeout = 120  # 2 minutes max wait
                poll_interval = 2   # check every 2 seconds
                elapsed = 0

                while elapsed < poll_timeout:
                    await _poll_asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                    interaction = await self.db.get_interaction(interaction_id)
                    if interaction and interaction.get("status") == "answered":
                        user_response = interaction.get("response", "")
                        await self._emit_progress("user_responded", f"User said: {user_response[:80]}")
                        return f"User's response: {user_response}"

                # Timeout — expire and move on
                await self.db.expire_interactions(self.task.id)
                await self._emit_progress("ask_timeout", "User didn't respond in time, proceeding with best judgment")
                return "User did not respond in time. Proceed with your best judgment and submit your artifact."

            elif name == "generate_image":
                if not self._media_gen:
                    return "Image generation not available for this task type."
                prompt = args.get("prompt", "")
                style = args.get("style", "professional")
                result = await self._media_gen.generate_image(prompt, style)
                if result.success:
                    return (
                        f"Image generated ({result.provider}):\n"
                        f"Type: {result.media_type}\n"
                        f"Description: {result.description}\n"
                        f"Content: [base64 data, {len(result.content)} chars]"
                    )
                return f"Image generation failed: {result.error}. Create a text description instead."

            elif name == "github_push":
                if not self._github_tool:
                    return "GitHub integration not available for this task type."
                if not self._github_tool.available:
                    return "GITHUB_TOKEN not configured. Include the code in your artifact instead."
                result = await self._github_tool.create_or_update_file(
                    repo=args.get("repo", ""),
                    path=args.get("path", ""),
                    content=args.get("content", ""),
                    message=args.get("message", "Update via Swarm Agents"),
                )
                if result.success:
                    return f"Pushed to GitHub: {result.data.get('url', result.data.get('path', ''))}"
                return f"GitHub push failed: {result.error}"

            elif name == "council_deliberate":
                if not self._council:
                    return "Council deliberation not available."
                question = args.get("question", "")
                context = args.get("context", "")
                result = await self._council.deliberate(question, context)
                output = f"## Council Deliberation ({len(result.votes)} models, {result.total_latency_ms}ms)\n\n"
                output += f"**Agreement Score:** {result.agreement_score:.0%}\n\n"
                for v in result.votes:
                    status = f"({v.latency_ms}ms)" if not v.error else f"(FAILED: {v.error[:50]})"
                    output += f"### {v.model} {status}\n{v.content[:500]}\n\n"
                output += f"## Synthesis\n{result.synthesis}\n\n"
                output += f"**Chosen Approach:** {result.chosen_approach}\n"
                return output

            elif name == "submit_artifact":
                self._submitted_artifact = args
                return "Artifact submitted successfully."

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return f"Error executing {name}: {str(e)[:500]}"

    def _build_artifact_from_submission(self, submission: dict) -> Artifact:
        """Build an Artifact from a submit_artifact tool call."""
        output_type = TASK_OUTPUT_MAP.get(self.task.type, ArtifactType.DOCUMENTATION)

        raw_tags = submission.get("tags", [])
        # Handle LLMs that pass tags as a string instead of array
        if isinstance(raw_tags, str):
            raw_tags = [t.strip().strip('"').strip("'") for t in raw_tags.split(",") if t.strip()]
        tags = list(raw_tags)
        tags.append(self.task.type.value)
        if self.task.payload.get("component"):
            tags.append(self.task.payload["component"].lower().replace(" ", "_"))
        tags = list(set(tags))

        metadata = submission.get("metadata") or {}
        if submission.get("file_path"):
            metadata["file_path"] = submission["file_path"]

        return Artifact(
            project_id=self.task.project_id,
            task_id=self.task.id,
            agent_id=self.identity.id,
            type=output_type,
            name=submission.get("name", f"{self.task.type.value}_{self.task.id[:8]}"),
            content=submission.get("content", ""),
            tags=tags,
            metadata=metadata,
        )

    def _serialize_assistant_message(self, message: Any) -> dict:
        """Serialize a litellm Message object to a dict for the messages list."""
        msg: dict[str, Any] = {"role": "assistant"}
        if message.content:
            msg["content"] = message.content
        if hasattr(message, 'tool_calls') and message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        return msg

    # ── LLM Calls ──────────────────────────────────────────────────

    async def _call_llm_with_tools(self, messages: list[dict]) -> Any:
        """Call the LLM with tool definitions via litellm function calling."""
        import asyncio as _asyncio
        import os

        model, api_base, api_key = self._resolve_model_params()
        llm_timeout = 90
        is_reasoning = self._is_reasoning_model(model)

        try:
            call_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": self._max_tokens,
                "temperature": self._model_config.temperature,
                "api_key": api_key,
                "timeout": llm_timeout,
            }
            if api_base:
                call_kwargs["api_base"] = api_base

            # Reasoning models don't support tools — use simple call
            if is_reasoning:
                call_kwargs.pop("temperature", None)  # Some reasoning models reject temperature
            elif self._tool_schemas and self._model_supports_tools(model):
                call_kwargs["tools"] = self._tool_schemas
                call_kwargs["tool_choice"] = "auto"

            response = await _asyncio.wait_for(
                litellm.acompletion(**call_kwargs),
                timeout=llm_timeout + 10,
            )

            # Fix reasoning models that return content=null
            response = self._fix_reasoning_response(response)
            return response

        except _asyncio.TimeoutError:
            err = f"LLM call timed out after {llm_timeout}s (model={model})"
            logger.error(f"Agent {self.identity.id}: {err}")
            raise TimeoutError(err)

    async def _call_llm_simple(self, messages: list[dict]) -> str:
        """Simple LLM call without tools (for fallback/final output)."""
        import asyncio as _asyncio

        model, api_base, api_key = self._resolve_model_params()

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._model_config.temperature,
            "api_key": api_key,
            "timeout": 90,
        }
        if api_base:
            call_kwargs["api_base"] = api_base

        # Reasoning models may not accept temperature
        if self._is_reasoning_model(model):
            call_kwargs.pop("temperature", None)

        response = await _asyncio.wait_for(
            litellm.acompletion(**call_kwargs),
            timeout=100,
        )
        response = self._fix_reasoning_response(response)
        return response.choices[0].message.content or ""

    def _resolve_model_params(self) -> tuple[str, str | None, str]:
        """Resolve the actual model string, api_base, and api_key for litellm."""
        import os

        model = self._model_config.model
        api_base = self._model_config.api_base
        api_key = os.getenv(self._model_config.api_key_env, "") or config.llm_api_key

        # NVIDIA NIM: use openai/ prefix for litellm
        if api_base and "nvidia" in api_base:
            if not model.startswith("openai/"):
                model = f"openai/{model}"
            os.environ["OPENAI_API_KEY"] = api_key

        # Set provider-specific env vars
        if "ANTHROPIC" in self._model_config.api_key_env:
            os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        elif "OPENAI" in self._model_config.api_key_env:
            os.environ.setdefault("OPENAI_API_KEY", api_key)

        return model, api_base, api_key

    def _model_supports_tools(self, model: str) -> bool:
        """Check if the model supports function calling."""
        # Reasoning models don't support tools
        if self._is_reasoning_model(model):
            return False
        # Most modern models support function calling via litellm
        no_tools = ["claude-3-haiku", "llama-2", "mistral-7b"]
        return not any(nt in model.lower() for nt in no_tools)

    def _is_reasoning_model(self, model: str) -> bool:
        """Check if this is a reasoning/chain-of-thought model that returns content differently."""
        reasoning_patterns = [
            "nemotron",
            "magistral",
            "phi-4-mini-flash-reasoning",
            "reasoning",
        ]
        m = model.lower()
        return any(p in m for p in reasoning_patterns)

    def _fix_reasoning_response(self, response: Any) -> Any:
        """
        Fix reasoning models that return content=null.
        These models put output in reasoning_content, thinking, or other fields.
        Extract the actual content so downstream code works normally.
        """
        try:
            message = response.choices[0].message
            content = message.content

            if content:
                # Strip <think>...</think> tags if present (Phi-4, etc.)
                if "<think>" in content:
                    import re
                    # Extract text outside think tags
                    cleaned = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                    if cleaned:
                        message.content = cleaned
                return response

            # content is None/empty — try to extract from reasoning fields
            # litellm sometimes exposes reasoning_content
            reasoning = getattr(message, 'reasoning_content', None)
            if reasoning:
                message.content = reasoning
                logger.info(f"Agent {self.identity.id}: Extracted content from reasoning_content field")
                return response

            # Check for thinking field (some models)
            thinking = getattr(message, 'thinking', None)
            if thinking:
                message.content = thinking
                logger.info(f"Agent {self.identity.id}: Extracted content from thinking field")
                return response

            # Check model_extra for any content-like fields
            extra = getattr(message, 'model_extra', {}) or {}
            for key in ('reasoning_content', 'thinking', 'reasoning', 'thought'):
                if key in extra and extra[key]:
                    message.content = extra[key]
                    logger.info(f"Agent {self.identity.id}: Extracted content from model_extra[{key}]")
                    return response

            # Last resort: check if there are tool_calls (valid empty content)
            if getattr(message, 'tool_calls', None):
                return response

            # Truly empty — set a fallback
            logger.warning(f"Agent {self.identity.id}: Reasoning model returned null content, no fallback found")
            message.content = "(Model returned empty response — reasoning model may need different handling)"

        except Exception as e:
            logger.warning(f"Error fixing reasoning response: {e}")

        return response

    # ── Prompt Building ────────────────────────────────────────────

    async def _build_system_prompt(
        self, env_context: dict, relevant_artifacts: list[dict]
    ) -> str:
        """Build the system prompt with persona + context + tool instructions."""
        from swarm.personas import get_persona_prompt

        persona_prompt = get_persona_prompt(self.identity.role)
        memory_context = await self.memory.get_context_window(
            task_summary=f"{self.task.type.value}: {json.dumps(self.task.payload)}",
            env_context=env_context,
        )

        # Tool instructions
        tool_names = get_tool_names_for_task(self.task.type)
        tool_section = ""
        if tool_names:
            tool_section = (
                "\n\n# Available Tools\n"
                f"You have access to these tools: {', '.join(tool_names)}\n\n"
                "IMPORTANT: When your work is complete, you MUST call the `submit_artifact` tool "
                "with your final output. This is how you deliver your work.\n\n"
                "You can use tools iteratively — search the web, read existing artifacts, "
                "write files, execute code to verify it works, then submit your final artifact.\n"
            )

            if "write_file" in tool_names:
                tool_section += (
                    "\nWhen writing code, use `write_file` to create actual project files with "
                    "proper paths (e.g., 'src/api/routes.py', 'src/models/user.py'). "
                    "Then include the main file content in your `submit_artifact` call.\n"
                )

            if "run_python" in tool_names:
                tool_section += (
                    "\nYou can use `run_python` to test your code before submitting. "
                    "If it fails, fix the issues and try again.\n"
                )

        parts = [
            persona_prompt,
            "",
            "# Your Identity",
            f"Agent ID: {self.identity.id}",
            f"Role: {self.identity.persona}",
            f"Task: {self.task.type.value}",
            f"Model: {self._model_config.model}",
            tool_section,
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

        payload = self.task.payload
        if payload:
            parts.append("## Task Details")
            for key, val in payload.items():
                parts.append(f"- **{key}**: {val}")
            parts.append("")

        if relevant_artifacts:
            parts.append("## Relevant Artifacts from Other Agents")
            for art in relevant_artifacts[:10]:
                parts.append(f"### {art['name']} ({art['type']})")
                parts.append(f"Tags: {art.get('tags', [])}")
                content = art.get("content", "")
                if len(content) > 3000:
                    content = content[:3000] + "\n... [truncated]"
                parts.append(f"```\n{content}\n```")
                parts.append("")

        output_type = TASK_OUTPUT_MAP.get(self.task.type, ArtifactType.DOCUMENTATION)
        parts.append("## Output Requirements")
        parts.append(f"Produce a **{output_type.value}** artifact.")
        parts.append("Use the `submit_artifact` tool to deliver your work.")

        # Fallback instructions for models without function calling
        parts.append("\nIf you cannot use tools, structure your output as:")
        parts.append("```")
        parts.append("ARTIFACT_NAME: <descriptive name>")
        parts.append("ARTIFACT_TAGS: <comma-separated tags>")
        parts.append("ARTIFACT_CONTENT:")
        parts.append("<your full output here>")
        parts.append("```")

        if self.task.type == TaskType.PLAN_ARCHITECTURE:
            parts.append("")
            parts.append("## CRITICAL: Architecture Decomposition")
            parts.append("Include a COMPONENTS array in your artifact metadata.")
            parts.append("This tells the swarm which agents to spawn next.")
            parts.append('Use submit_artifact with metadata: {"components": [...]}')
            parts.append("Each component: {\"name\": \"...\", \"type\": \"design_database|create_api|design_ui|write_code|build_frontend_component|deploy|write_tests\", \"description\": \"...\", \"priority\": 1-3}")

        if self.task.type == TaskType.REVIEW_CODE:
            parts.append("")
            parts.append("## Review Instructions")
            parts.append('Include in metadata: {"verdict": "PASS" or "FAIL", "has_issues": true/false, "issues": [...]}')

        return "\n".join(parts)

    # ── Legacy Output Parsing (fallback for non-tool-calling models) ────

    def _parse_output(self, llm_response: str) -> Artifact:
        """Parse LLM response into an Artifact (legacy mode)."""
        output_type = TASK_OUTPUT_MAP.get(self.task.type, ArtifactType.DOCUMENTATION)

        name = self._extract_field(llm_response, "ARTIFACT_NAME") or f"{self.task.type.value}_{self.task.id[:8]}"
        tags_str = self._extract_field(llm_response, "ARTIFACT_TAGS") or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        content = self._extract_content(llm_response)

        tags.append(self.task.type.value)
        if self.task.payload.get("component"):
            tags.append(self.task.payload["component"].lower().replace(" ", "_"))
        tags = list(set(tags))

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
            content = content.strip()
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            return content.strip()
        return text.strip()

    def _extract_components(self, text: str) -> list[dict]:
        marker = "COMPONENTS:"
        idx = text.find(marker)
        if idx < 0:
            return []
        json_str = text[idx + len(marker):].strip()
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
