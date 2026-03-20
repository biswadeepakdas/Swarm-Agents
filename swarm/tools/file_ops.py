"""
File I/O tool — agents can read, write, and create files in the project output directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiofiles

logger = logging.getLogger("swarm.tools.file_ops")


class FileOps:
    def __init__(self, project_output_dir: str | Path) -> None:
        self.base_dir = Path(project_output_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        resolved = (self.base_dir / relative_path).resolve()
        if not str(resolved).startswith(str(self.base_dir.resolve())):
            raise ValueError(f"Path traversal denied: {relative_path}")
        return resolved

    async def write_file(self, relative_path: str, content: str) -> str:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w") as f:
            await f.write(content)
        logger.info(f"File written: {path}")
        return str(path)

    async def read_file(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        async with aiofiles.open(path, "r") as f:
            return await f.read()

    async def list_files(self, relative_dir: str = ".") -> list[str]:
        path = self._resolve(relative_dir)
        if not path.is_dir():
            return []
        files = []
        for item in sorted(path.rglob("*")):
            if item.is_file():
                files.append(str(item.relative_to(self.base_dir)))
        return files

    async def file_exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).exists()

    async def create_directory(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
