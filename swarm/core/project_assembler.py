"""
Project Assembler — packages all artifacts into a downloadable project.

Takes all artifacts from a completed project and assembles them into
a structured file tree with proper paths, then creates a ZIP archive.
Like Perplexity Computer's "deliver finished artifacts."
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.db.postgres import PostgresDB

logger = logging.getLogger("swarm.project_assembler")


# Map artifact types to default file paths when no explicit path is set
DEFAULT_PATHS: dict[str, str] = {
    "requirements_doc": "docs/requirements.md",
    "architecture_plan": "docs/architecture.md",
    "database_schema": "database/schema.sql",
    "code_file": "src/{name}",
    "frontend_component": "frontend/src/components/{name}",
    "ui_design": "docs/ui_design.md",
    "test_suite": "tests/{name}",
    "deployment_config": "deploy/{name}",
    "documentation": "docs/{name}",
    "review": "reviews/{name}.md",
    "api_spec": "docs/api_spec.md",
    "decision": "docs/decisions/{name}.md",
    "bug_report": "docs/bugs/{name}.md",
}


def _guess_extension(artifact: dict) -> str:
    """Guess file extension from artifact content and tags."""
    tags = [t.lower() for t in (artifact.get("tags") or [])]
    content = (artifact.get("content") or "")[:200].lower()
    atype = artifact.get("type", "")

    if atype == "database_schema":
        return ".sql"
    if atype in ("requirements_doc", "architecture_plan", "ui_design", "review", "documentation"):
        return ".md"
    if atype == "deployment_config":
        if "docker" in content or "dockerfile" in " ".join(tags):
            return ""  # Dockerfile has no extension
        if "yaml" in content or "yml" in " ".join(tags):
            return ".yml"
        return ".yml"

    # Code files — guess from tags
    if any(t in tags for t in ["python", "fastapi", "flask", "django"]):
        return ".py"
    if any(t in tags for t in ["typescript", "tsx", "react"]):
        return ".tsx"
    if any(t in tags for t in ["javascript", "express", "node"]):
        return ".js"
    if any(t in tags for t in ["go", "golang"]):
        return ".go"
    if any(t in tags for t in ["rust"]):
        return ".rs"
    if any(t in tags for t in ["java", "spring"]):
        return ".java"
    if any(t in tags for t in ["sql"]):
        return ".sql"
    if any(t in tags for t in ["html"]):
        return ".html"
    if any(t in tags for t in ["css", "tailwind"]):
        return ".css"

    # Guess from content
    if "from fastapi" in content or "import asyncio" in content or "def " in content:
        return ".py"
    if "import React" in content or "export " in content:
        return ".tsx"
    if "const " in content and ("require(" in content or "express" in content):
        return ".js"

    return ".txt"


def _sanitize_filename(name: str) -> str:
    """Convert artifact name to a safe filename."""
    # Remove special chars, replace spaces with underscores
    name = re.sub(r'[^\w\s\-.]', '', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('_').lower()
    return name[:80] or "unnamed"


def _deduplicate_path(path: str, used_paths: set[str]) -> str:
    """If path is already used, append a number."""
    if path not in used_paths:
        return path
    base, ext = path.rsplit('.', 1) if '.' in path else (path, '')
    for i in range(2, 100):
        candidate = f"{base}_{i}.{ext}" if ext else f"{base}_{i}"
        if candidate not in used_paths:
            return candidate
    return f"{base}_{99}.{ext}" if ext else f"{base}_{99}"


async def assemble_project(db: PostgresDB, project_id: str) -> dict[str, Any]:
    """
    Assemble all artifacts into a file tree structure.
    Returns: { "files": [{"path": ..., "content": ..., "type": ...}], "manifest": {...} }
    """
    # Get project and artifacts
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")

    artifacts = await db.query_artifacts(str(project_id))
    if not artifacts:
        return {"files": [], "manifest": {"project": project.get("name"), "artifact_count": 0}}

    # Ensure all artifacts are dicts (handle edge cases)
    clean_artifacts = []
    for a in artifacts:
        if isinstance(a, dict):
            # Ensure tags is a list, not a string
            tags = a.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            a["tags"] = tags
            clean_artifacts.append(a)
    artifacts = clean_artifacts

    files = []
    used_paths = set()

    # Sort: requirements first, then architecture, then code, then reviews
    type_order = {
        "requirements_doc": 0, "architecture_plan": 1, "database_schema": 2,
        "code_file": 3, "frontend_component": 3, "api_spec": 4,
        "test_suite": 5, "deployment_config": 6, "ui_design": 7,
        "documentation": 8, "review": 9, "decision": 10, "bug_report": 11,
    }
    sorted_artifacts = sorted(artifacts, key=lambda a: type_order.get(a.get("type", ""), 99))

    # Deduplicate: keep only the latest artifact of each name+type combo
    seen = set()
    unique_artifacts = []
    for art in sorted_artifacts:
        key = (art.get("type", ""), _sanitize_filename(art.get("name", "")))
        if key not in seen:
            seen.add(key)
            unique_artifacts.append(art)

    for art in unique_artifacts:
        atype = art.get("type", "unknown")
        content = art.get("content", "")
        if not content:
            continue

        # Check if artifact has an explicit file_path in metadata
        metadata = art.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        explicit_path = metadata.get("file_path")

        if explicit_path:
            file_path = explicit_path
        else:
            # Use default path template
            template = DEFAULT_PATHS.get(atype, "output/{name}")
            safe_name = _sanitize_filename(art.get("name", "unnamed"))
            ext = _guess_extension(art)
            file_path = template.replace("{name}", safe_name + ext)

        file_path = _deduplicate_path(file_path, used_paths)
        used_paths.add(file_path)

        files.append({
            "path": file_path,
            "content": content,
            "type": atype,
            "artifact_name": art.get("name", ""),
            "tags": art.get("tags", []),
        })

    # Generate README
    project_name = project.get("name", "Swarm Project")
    brief = project.get("brief", "")
    readme = _generate_readme(project_name, brief, files)
    files.insert(0, {
        "path": "README.md",
        "content": readme,
        "type": "documentation",
        "artifact_name": "README",
        "tags": ["readme"],
    })

    manifest = {
        "project": project_name,
        "brief": brief,
        "artifact_count": len(unique_artifacts),
        "file_count": len(files),
        "types": list(set(f["type"] for f in files)),
    }

    return {"files": files, "manifest": manifest}


def _generate_readme(project_name: str, brief: str, files: list[dict]) -> str:
    """Generate a README.md for the assembled project."""
    lines = [
        f"# {project_name}",
        "",
        brief,
        "",
        "---",
        "",
        "## Project Structure",
        "",
        "```",
    ]

    # Build tree
    for f in sorted(files, key=lambda x: x["path"]):
        lines.append(f"  {f['path']}")

    lines.extend([
        "```",
        "",
        "## Files",
        "",
    ])

    # Group by type
    by_type: dict[str, list[dict]] = {}
    for f in files:
        t = f["type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(f)

    for ftype, flist in by_type.items():
        lines.append(f"### {ftype.replace('_', ' ').title()}")
        for f in flist:
            lines.append(f"- `{f['path']}` — {f['artifact_name']}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "*Generated by Swarm Agents*",
    ])

    return "\n".join(lines)


async def create_zip(db: PostgresDB, project_id: str) -> bytes:
    """Create a ZIP archive of the assembled project."""
    result = await assemble_project(db, project_id)
    files = result["files"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f["path"], f["content"])

        # Add manifest
        zf.writestr("manifest.json", json.dumps(result["manifest"], indent=2))

    return buf.getvalue()
