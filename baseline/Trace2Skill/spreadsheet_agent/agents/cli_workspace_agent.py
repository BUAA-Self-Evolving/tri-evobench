"""General workspace agent used for non-spreadsheet Trace2Skill turns."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from react_agent import Tool

from .base import BaseSpreadsheetAgent
from ..tools import create_bash_tool, create_python_tool


WORKSPACE_SYSTEM_TEMPLATE = """You are an execution agent for general files in a benchmark workspace.

The task gives you a working_directory and an instruction. Complete the
instruction by inspecting and modifying files inside that directory only.
The request may involve plain text, JSON, configuration, or other files; do
not assume that an input spreadsheet exists unless the instruction names one.

## Workspace path mapping

In benchmark instructions, `/workspace/foo` means the file
`working_directory/foo` directly. The working_directory itself is the mounted
`/workspace`; do NOT create an extra `workspace` subdirectory. If the task is
only asking for a textual answer and does not request a file change, answer it
directly and do not create placeholder files.

Use the available tools to inspect the actual workspace and perform the
requested changes. Prefer the python tool for reliable cross-platform file
operations: pass it a complete Python program using pathlib and json. The
python tool runs with the task working directory as its current directory.

## Safety and completion

- Read and write only under the provided working_directory.
- Preserve unspecified content when the task asks for an edit.
- Do not create unrelated files or claim a change that was not performed.
- After verifying the requested result, output exactly `ACTION: TASK_COMPLETE`.

## Action format

Thought: <current evidence and next step>

Action:
{{
    "name": "<python or bash>",
    "arguments": {{"code": "..."}}
}}

For the bash tool the argument is `command` instead of `code`. Do not use
Markdown fences around the action. Wait for the observation after every
action.

Available tools:
{{tool_definitions}}
"""


class CLIWorkspaceAgent(BaseSpreadsheetAgent):
    """Small general-purpose file agent sharing Trace2Skill's ReAct loop."""

    def __init__(
        self,
        client,
        max_turns: int = 20,
        temperature: float = 0.0,
        verbose: bool = True,
        timeout: int = 120,
        log_dir: str | None = None,
        log_format: str = "markdown",
    ):
        super().__init__(client, max_turns, temperature, verbose, log_dir, log_format)
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "cli_workspace_agent"

    def get_system_prompt(self) -> str:
        return WORKSPACE_SYSTEM_TEMPLATE

    def get_system_template(self) -> str:
        return WORKSPACE_SYSTEM_TEMPLATE

    def requires_output_file(self) -> bool:
        return False

    def create_tools(self, working_dir: str) -> list[Tool]:
        # Python is available on every supported host and avoids shell quoting
        # differences. Bash remains available for compatibility with the
        # upstream Trace2Skill protocol and for simple shell diagnostics.
        return [
            create_python_tool(working_dir, timeout=self.timeout),
            create_bash_tool(working_dir, timeout=self.timeout),
        ]

    def build_task_prompt(self, context) -> str:
        working_dir = os.path.abspath(context.working_dir)
        direct_answer_hint = ""
        if "/workspace/" not in context.instruction.lower() and not any(
            verb in context.instruction.lower()
            for verb in ("create", "write", "edit", "update", "repair", "remove", "delete", "save")
        ):
            direct_answer_hint = "\nThis is a response-only task: answer the instruction directly without using tools or creating files."
        return f"""### working_directory
{working_dir}

### instruction
{context.instruction}

Only access files within `{working_dir}`. Inspect the current files before
editing when necessary, then verify the requested result and finish with
`ACTION: TASK_COMPLETE`.{direct_answer_hint}"""
