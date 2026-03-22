"""
Tool Registry — defines all tools agents can call via function calling.

Each tool has:
- A JSON schema (for litellm function calling)
- A callable handler (async function)
- Per-task-type availability

This is what transforms agents from one-shot prompt→artifact converters
into iterative tool-using agents (like Hive Computer).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Awaitable

from swarm.models.task import TaskType

logger = logging.getLogger("swarm.tools.registry")


# Tool schema definitions (OpenAI function calling format)
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute Python code in a sandboxed subprocess. Returns stdout, stderr, and exit code. Use this to test code, run calculations, or validate implementations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for documentation, packages, APIs, best practices, or any information. Returns title, URL, and snippet for each result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file to the project output directory. Use this to create actual project files (source code, configs, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to project root (e.g., 'src/api/routes.py')",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project output directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to project root",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in the project output directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory path relative to project root (default '.')",
                        "default": ".",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_artifacts",
            "description": "Search existing artifacts produced by other agents in this project. Find code, designs, schemas, etc. by type or tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_type": {
                        "type": "string",
                        "description": "Filter by type: code_file, architecture_plan, database_schema, ui_design, requirements_doc, review, test_suite, deployment_config, frontend_component, documentation",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (matches any)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a clarifying question when you're unsure about requirements, design decisions, or need approval. The agent will pause until the user responds. Use sparingly — only when the answer significantly affects your output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of suggested answers (multiple choice). If provided, the user can pick one or type a custom response.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Brief context explaining why you need this information",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch and extract the main text content from a web page URL. Returns cleaned text, useful for reading documentation, articles, API references, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the web page to fetch and extract text from",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image or visual asset from a text description. Returns an image (or SVG placeholder if no image API key is configured). Use for UI mockups, diagrams, logos, illustrations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Description of the image to generate",
                    },
                    "style": {
                        "type": "string",
                        "description": "Visual style: professional, minimalist, colorful, dark, etc.",
                        "default": "professional",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_push",
            "description": "Push a file to a GitHub repository. Requires GITHUB_TOKEN env var. Creates or updates a file in the specified repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository full name (e.g., 'user/repo-name')",
                    },
                    "path": {
                        "type": "string",
                        "description": "File path in the repo (e.g., 'src/main.py')",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to push",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message",
                        "default": "Update via Swarm Agents",
                    },
                },
                "required": ["repo", "path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "council_deliberate",
            "description": "Run a multi-model council deliberation on a high-impact question. Sends the question to 2-4 different LLMs, compares responses, and synthesizes a recommendation. Use for architecture decisions, security trade-offs, or design choices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question or decision to deliberate on",
                    },
                    "context": {
                        "type": "string",
                        "description": "Background context for the decision",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_artifact",
            "description": "Submit your final artifact (code, design, review, etc.). Call this when your work is complete. This is REQUIRED — you must submit exactly one artifact.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Descriptive name for the artifact",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full artifact content (code, document, schema, etc.)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for discoverability (e.g., ['python', 'fastapi', 'rest_api'])",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional file path in project output (e.g., 'src/api/routes.py'). If set, also writes the file.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata (e.g., components for architecture plans, verdict for reviews)",
                    },
                },
                "required": ["name", "content", "tags"],
            },
        },
    },
]


# Which tools each task type can access
TASK_TOOL_MAP: dict[TaskType, list[str]] = {
    # Research/planning agents: search + browse + ask_user + query
    TaskType.ANALYZE_REQUIREMENTS: ["web_search", "fetch_page", "ask_user", "query_artifacts", "submit_artifact"],
    TaskType.PLAN_ARCHITECTURE: ["web_search", "fetch_page", "ask_user", "council_deliberate", "query_artifacts", "submit_artifact"],
    TaskType.RESEARCH: ["web_search", "fetch_page", "query_artifacts", "submit_artifact"],

    # Build agents: full toolset (code + files + search + browse + ask_user)
    TaskType.WRITE_CODE: ["run_python", "web_search", "fetch_page", "write_file", "read_file", "list_files", "query_artifacts", "submit_artifact"],
    TaskType.CREATE_API: ["run_python", "web_search", "fetch_page", "write_file", "read_file", "list_files", "query_artifacts", "submit_artifact"],
    TaskType.DESIGN_DATABASE: ["web_search", "fetch_page", "ask_user", "write_file", "query_artifacts", "submit_artifact"],
    TaskType.BUILD_FRONTEND_COMPONENT: ["web_search", "fetch_page", "write_file", "read_file", "list_files", "query_artifacts", "submit_artifact"],
    TaskType.FIX_CODE: ["run_python", "web_search", "fetch_page", "write_file", "read_file", "list_files", "query_artifacts", "submit_artifact"],
    TaskType.DEPLOY: ["write_file", "query_artifacts", "submit_artifact"],

    # Design agents: search + browse + ask_user + query
    TaskType.DESIGN_UI: ["web_search", "fetch_page", "ask_user", "query_artifacts", "submit_artifact"],

    # QA agents: code execution + query
    TaskType.REVIEW_CODE: ["run_python", "query_artifacts", "submit_artifact"],
    TaskType.WRITE_TESTS: ["run_python", "write_file", "read_file", "query_artifacts", "submit_artifact"],
    TaskType.INTEGRATION_TEST: ["run_python", "write_file", "read_file", "query_artifacts", "submit_artifact"],
    TaskType.DEBUG: ["run_python", "web_search", "fetch_page", "read_file", "query_artifacts", "submit_artifact"],

    # Docs
    TaskType.WRITE_DOCS: ["web_search", "fetch_page", "query_artifacts", "write_file", "submit_artifact"],
    TaskType.RESOLVE_CONFLICT: ["ask_user", "query_artifacts", "submit_artifact"],

    # New: Hive Computer spec
    TaskType.EVALUATE_PROJECT: ["query_artifacts", "run_python", "council_deliberate", "submit_artifact"],
    TaskType.ASSEMBLE_DELIVERABLES: ["query_artifacts", "read_file", "list_files", "write_file", "submit_artifact"],
    TaskType.GENERATE_MEDIA: ["generate_image", "web_search", "query_artifacts", "submit_artifact"],
    TaskType.COUNCIL_REVIEW: ["council_deliberate", "query_artifacts", "web_search", "submit_artifact"],
}


def get_tools_for_task(task_type: TaskType) -> list[dict[str, Any]]:
    """Get the tool schemas available for a given task type."""
    allowed_names = TASK_TOOL_MAP.get(task_type, ["query_artifacts", "submit_artifact"])
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] in allowed_names]


def get_tool_names_for_task(task_type: TaskType) -> list[str]:
    """Get tool names available for a given task type."""
    return TASK_TOOL_MAP.get(task_type, ["query_artifacts", "submit_artifact"])
