"""
Bash command execution tool.
"""

import subprocess
import sys
import os
import shutil
from .process import run_bounded

# Add parent src to path for react_agent imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from react_agent import tool


def create_bash_tool(working_dir: str, timeout: int = 120):
    """
    Create a bash execution tool for running commands.

    Args:
        working_dir: Directory where commands will be executed
        timeout: Command timeout in seconds
    """

    @tool(name="bash")
    def bash(command: str) -> str:
        """
        Execute a bash command in the working directory.
        Use this to run Python scripts, install packages, navigate files,
        or perform any shell operations.

        Args:
            command: The bash command to execute
        """
        try:
            docker_exe = os.environ.get("DOCKER_EXE") or shutil.which("docker")
            if os.name == "nt" and docker_exe:
                result = run_bounded(
                    [
                        docker_exe, "run", "--rm",
                        "-v", f"{os.path.abspath(working_dir)}:/workspace",
                        "-w", "/workspace", os.environ.get("TRIEVO_TOOL_IMAGE", "trievo-ubuntu:22.04"),
                        "bash", "-lc", command,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            else:
                result = run_bounded(
                    command,
                    shell=True,
                    cwd=working_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}" if output else result.stderr
            if result.returncode != 0:
                output += f"\n[Exit code: {result.returncode}]"
            return output.strip() if output.strip() else "[Command completed with no output]"
        except subprocess.TimeoutExpired:
            return f"[ERROR] Command timed out after {timeout} seconds"
        except Exception as e:
            return f"[ERROR] Failed to execute command: {e}"

    return bash
