"""Python code execution tool for cross-platform agent runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from .process import run_bounded

from react_agent import tool


def create_python_tool(working_dir: str, timeout: int = 120):
    """Create a tool that executes a Python snippet in ``working_dir``."""

    @tool(name="python")
    def python_code(code: str) -> str:
        """
        Execute Python code in the working directory.

        Args:
            code: A complete Python program or a Python snippet to execute.
        """
        try:
            docker_exe = os.environ.get("DOCKER_EXE") or shutil.which("docker")
            # On Windows, /workspace is meaningful only inside the Linux
            # sandbox.  Running the snippet through the same mounted
            # container as the bash tool keeps absolute benchmark paths and
            # relative paths pointed at the exact same workspace.
            command = [sys.executable, "-c", code]
            cwd = working_dir
            if os.name == "nt" and docker_exe:
                command = [
                    docker_exe, "run", "--rm",
                    "-v", f"{os.path.abspath(working_dir)}:/workspace",
                    "-w", "/workspace", os.environ.get("TRIEVO_TOOL_IMAGE", "trievo-ubuntu:22.04"),
                    "python3", "-c", code,
                ]
                cwd = None
            result = run_bounded(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            output = result.stdout or ""
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            return output.strip() if output.strip() else "[Python completed with no output]"
        except subprocess.TimeoutExpired:
            return f"[ERROR] Python execution timed out after {timeout} seconds"
        except Exception as exc:
            return f"[ERROR] Failed to execute Python: {exc}"

    return python_code
