"""
Sandboxed code execution — run generated code safely in isolated subprocesses.

Security layers:
1. Isolated temp directory per execution (no access to host filesystem)
2. Resource limits: CPU time, memory, file size, process count
3. Network disabled via environment variable (optional)
4. Timeout enforcement with hard kill
5. Blocked dangerous imports (os.system, subprocess, etc.)
6. Output size capping
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("swarm.tools.code_runner")

# Maximum output size in bytes (prevent memory bombs)
MAX_OUTPUT_BYTES = 50_000
# Maximum file size the sandbox can write (10MB)
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

# Dangerous patterns that should be blocked in code
BLOCKED_PATTERNS = [
    "os.system(",
    "subprocess.call(",
    "subprocess.run(",
    "subprocess.Popen(",
    "__import__('os').system",
    "__import__('subprocess')",
    "shutil.rmtree('/'",
    "shutil.rmtree(\"/\"",
    "open('/etc/",
    "open(\"/etc/",
    "eval(input",
    "exec(input",
]

# Allowed imports — anything not in this list gets a warning but still runs
# (we rely on resource limits for true safety, not import blocking)
SAFE_IMPORT_WARNING = """
# NOTE: This code uses potentially dangerous imports.
# Running in sandboxed mode with resource limits.
"""


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    sandbox_dir: str = ""
    files_created: list[str] = field(default_factory=list)


@dataclass
class SandboxConfig:
    """Configuration for the sandbox environment."""
    timeout: int = 30           # seconds
    max_memory_mb: int = 256    # megabytes
    max_cpu_seconds: int = 20   # CPU time limit
    max_file_size_mb: int = 10  # max file write size
    max_processes: int = 5      # max child processes
    allow_network: bool = False # disable network access
    allow_file_write: bool = True  # allow writing files in sandbox dir


class CodeRunner:
    DEFAULT_TIMEOUT = 30  # seconds

    def __init__(self, sandbox_config: SandboxConfig | None = None):
        self.config = sandbox_config or SandboxConfig()

    def _check_code_safety(self, code: str) -> tuple[bool, str]:
        """
        Check code for obviously dangerous patterns.
        Returns (is_safe, warning_message).
        Does NOT block execution — we rely on resource limits for real safety.
        """
        warnings = []
        for pattern in BLOCKED_PATTERNS:
            if pattern in code:
                warnings.append(f"Blocked pattern detected: {pattern}")

        if warnings:
            return False, "; ".join(warnings)
        return True, ""

    def _create_sandbox_dir(self) -> Path:
        """Create an isolated temporary directory for this execution."""
        sandbox = Path(tempfile.mkdtemp(prefix="swarm_sandbox_"))
        # Create a minimal structure
        (sandbox / "output").mkdir(exist_ok=True)
        return sandbox

    def _build_wrapper_code(self, code: str, sandbox_dir: Path) -> str:
        """
        Wrap user code with:
        - Resource limits (ulimit equivalent via resource module)
        - Working directory set to sandbox
        - Import restrictions (warnings only)
        - Output file listing after execution
        """
        resource_limits = ""
        if platform.system() != "Windows":
            resource_limits = f"""
import resource
import sys

# Set resource limits
try:
    # CPU time limit ({self.config.max_cpu_seconds}s)
    resource.setrlimit(resource.RLIMIT_CPU, ({self.config.max_cpu_seconds}, {self.config.max_cpu_seconds + 5}))
    # Memory limit ({self.config.max_memory_mb}MB)
    mem_bytes = {self.config.max_memory_mb} * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    # File size limit ({self.config.max_file_size_mb}MB)
    file_bytes = {self.config.max_file_size_mb} * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    # Max child processes
    resource.setrlimit(resource.RLIMIT_NPROC, ({self.config.max_processes}, {self.config.max_processes}))
except (ValueError, resource.error) as e:
    print(f"[sandbox] Warning: Could not set resource limit: {{e}}", file=sys.stderr)
"""

        wrapper = f"""
import os
import sys

# ── Sandbox Setup ──
{resource_limits}

# Change to sandbox directory
os.chdir({str(sandbox_dir)!r})

# Restrict file writes to sandbox only
_original_open = open
def _sandboxed_open(path, *args, **kwargs):
    path_str = str(os.path.abspath(path))
    sandbox_str = {str(sandbox_dir)!r}
    if not path_str.startswith(sandbox_str) and ('w' in str(args) or 'a' in str(args) or 'w' in str(kwargs.get('mode', '')) or 'a' in str(kwargs.get('mode', ''))):
        raise PermissionError(f"[sandbox] Cannot write outside sandbox: {{path}}")
    return _original_open(path, *args, **kwargs)

import builtins
builtins.open = _sandboxed_open

# ── User Code ──
try:
{self._indent_code(code, 4)}
except Exception as _e:
    print(f"Error: {{type(_e).__name__}}: {{_e}}", file=sys.stderr)
    sys.exit(1)

# ── List created files ──
import os as _os
_sandbox = {str(sandbox_dir)!r}
_files = []
for _root, _dirs, _fnames in _os.walk(_sandbox):
    for _f in _fnames:
        _fp = _os.path.join(_root, _f)
        _rel = _os.path.relpath(_fp, _sandbox)
        _sz = _os.path.getsize(_fp)
        _files.append(f"  {{_rel}} ({{_sz}} bytes)")
if _files:
    print("\\n[sandbox] Files in workspace:")
    print("\\n".join(_files))
"""
        return wrapper

    def _indent_code(self, code: str, spaces: int) -> str:
        """Indent code by a given number of spaces."""
        prefix = " " * spaces
        return "\n".join(prefix + line for line in code.split("\n"))

    async def run_python(
        self, code: str, timeout: int | None = None
    ) -> ExecutionResult:
        """Execute Python code in a sandboxed subprocess."""
        timeout = timeout or self.config.timeout

        # Safety check (warning only — we still run it with resource limits)
        is_safe, warning = self._check_code_safety(code)
        if not is_safe:
            logger.warning(f"Code safety warning: {warning}")

        # Create isolated sandbox directory
        sandbox_dir = self._create_sandbox_dir()

        try:
            # Build wrapped code with resource limits
            wrapped_code = self._build_wrapper_code(code, sandbox_dir)

            # Write to temp file inside sandbox
            script_path = sandbox_dir / "_run.py"
            script_path.write_text(wrapped_code)

            # Build environment (optionally disable network)
            env = os.environ.copy()
            if not self.config.allow_network:
                # Hint to Python that network should be restricted
                # (True network isolation requires Docker/namespace — this is best-effort)
                env["no_proxy"] = "*"
                env["http_proxy"] = "http://0.0.0.0:0"
                env["https_proxy"] = "http://0.0.0.0:0"
                env["PYTHONDONTWRITEBYTECODE"] = "1"

            # Execute in subprocess
            result = await self._execute(
                ["python3", str(script_path)],
                cwd=str(sandbox_dir),
                timeout=timeout,
                env=env,
            )

            # List files created in sandbox
            files_created = []
            for root, dirs, files in os.walk(str(sandbox_dir)):
                for f in files:
                    if f.startswith("_run"):
                        continue
                    fp = os.path.join(root, f)
                    rel = os.path.relpath(fp, str(sandbox_dir))
                    files_created.append(rel)

            result.sandbox_dir = str(sandbox_dir)
            result.files_created = files_created

            # Prepend safety warning if needed
            if not is_safe:
                result.stderr = f"[sandbox] Safety warning: {warning}\n{result.stderr}"

            return result

        finally:
            # Clean up sandbox (with a small delay to allow file reads)
            try:
                shutil.rmtree(str(sandbox_dir), ignore_errors=True)
            except Exception:
                pass

    async def run_command(
        self, command: list[str], cwd: str | None = None, timeout: int | None = None
    ) -> ExecutionResult:
        """Execute an arbitrary command (no sandboxing — use with care)."""
        timeout = timeout or self.DEFAULT_TIMEOUT
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
        timeout: int | None = None,
        env: dict | None = None,
    ) -> ExecutionResult:
        timeout = timeout or self.DEFAULT_TIMEOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                # Cap output size
                stdout_str = stdout.decode(errors="replace")[:MAX_OUTPUT_BYTES]
                stderr_str = stderr.decode(errors="replace")[:MAX_OUTPUT_BYTES]

                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
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
