"""
Code execution sandbox — run generated code safely in a subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("swarm.tools.code_runner")


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CodeRunner:
    DEFAULT_TIMEOUT = 30  # seconds

    async def run_python(
        self, code: str, timeout: int = DEFAULT_TIMEOUT
    ) -> ExecutionResult:
        """Execute Python code in a subprocess sandbox."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        try:
            return await self._execute(
                ["python3", tmp_path], timeout=timeout
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def run_command(
        self, command: list[str], cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT
    ) -> ExecutionResult:
        """Execute an arbitrary command."""
        return await self._execute(command, cwd=cwd, timeout=timeout)

    async def run_tests(
        self, test_dir: str, timeout: int = 60
    ) -> ExecutionResult:
        """Run pytest on a directory."""
        return await self._execute(
            ["python3", "-m", "pytest", test_dir, "-v", "--tb=short"],
            timeout=timeout,
        )

    async def _execute(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> ExecutionResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout.decode(errors="replace"),
                    stderr=stderr.decode(errors="replace"),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Execution timed out after {timeout}s",
                    timed_out=True,
                )
        except Exception as e:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
            )
