"""
ReAct format converter.

This module handles the conversion between:
- Tool definitions -> ReAct-style system prompts
- Model outputs -> Parsed actions
- Tool results -> Observation format

The ReAct format uses text-based "Action:" and "Observation:" markers
instead of native function calling, making it portable across different LLMs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import Tool
    from .prompts import SystemPromptBuilder


# System prompt template with few-shot examples
# Note: Double braces {{ }} are escaped braces for .format()
REACT_SYSTEM_TEMPLATE = '''You are an expert assistant who can solve any task using tool calls. You will be given a task to solve as best you can.
To do so, you have been given access to some tools.

The tool call you write is an action: after you output an Action, the tool will be executed and the user will provide the result as an "Observation:" message. You must wait for this observation before continuing - do NOT generate observations yourself.
This Action/Observation can repeat N times, you should take several steps when needed.

You can think step-by-step before taking an action.

## CRITICAL: Action Format Requirements

You MUST use this EXACT format for tool calls:

Action:
{{
    "name": "<tool_name>",
    "arguments": {{<arguments_as_json>}}
}}

IMPORTANT:
- The word "Action:" must appear on its own line, followed by a JSON object
- The JSON must have "name" (string) and "arguments" (object) fields
- Do NOT use markdown code blocks (```json or ```bash) around actions
- Do NOT write raw commands or code outside of the Action JSON format
- Any other format will NOT be parsed and the tool will NOT execute

## Completing the Task

When you have finished the task, signal completion by outputting exactly:

ACTION: TASK_COMPLETE

This tells the system you are done. Do NOT use any other method to end the task.

## Examples

Here are a few examples using notional tools:
---
Task: "What is the weather in Paris and what should I wear?"

Thought: I need to first get the weather in Paris, then provide clothing recommendations.

Action:
{{
    "name": "get_weather",
    "arguments": {{"city": "Paris"}}
}}

Observation: "Paris: 15C, partly cloudy, 60% humidity"

Thought: The weather is mild at 15C and partly cloudy. I can now give clothing advice.

Since it's 15C and partly cloudy in Paris, I recommend wearing layers - a light jacket or sweater over a t-shirt. Bring an umbrella just in case as it's partly cloudy.

ACTION: TASK_COMPLETE

---
Task: "Calculate 25 * 17 + 300"

Action:
{{
    "name": "calculator",
    "arguments": {{"expression": "25 * 17 + 300"}}
}}

Observation: "725"

The result of 25 * 17 + 300 is 725.

ACTION: TASK_COMPLETE

---
Above examples were using notional tools that might not exist for you. You only have access to these tools:
{tool_definitions}

Remember:
- Use "Action:" followed by a JSON object to call a tool - no other format works
- Wait for "Observation:" to see the result before proceeding
- When you have finished the task, output "ACTION: TASK_COMPLETE"
- Think step-by-step when the problem is complex'''


ACTION_TEMPLATE = '''Action:
{{
    "name": "{name}",
    "arguments": {arguments}
}}'''


# Task completion signal
TASK_COMPLETE_SIGNAL = "ACTION: TASK_COMPLETE"

# Format error template
FORMAT_ERROR_TEMPLATE = """Failed to parse your action. Please use the correct format.

To execute a tool, use this EXACT format:

Action:
{{
    "name": "<tool_name>",
    "arguments": {{<arguments_as_json>}}
}}

To complete the task, output exactly:

ACTION: TASK_COMPLETE

Please try again with the correct format."""


# Observation truncation settings
MAX_OBSERVATION_LENGTH = 6000
TRUNCATE_HEAD_CHARS = 3000
TRUNCATE_TAIL_CHARS = 3000


def should_skip_truncation(action_str: str | None, no_truncate_patterns: list[str] | None) -> bool:
    """
    Check if truncation should be skipped based on the action and patterns.
    
    Args:
        action_str: String representation of the action (e.g., the bash command)
        no_truncate_patterns: List of path patterns that should not be truncated
        
    Returns:
        True if truncation should be skipped, False otherwise
    """
    if not action_str or not no_truncate_patterns:
        return False
    
    # Check if any pattern appears in the action string
    for pattern in no_truncate_patterns:
        if pattern in action_str:
            return True
    
    return False


def truncate_observation(
    text: str,
    max_length: int = MAX_OBSERVATION_LENGTH,
    action_str: str | None = None,
    no_truncate_patterns: list[str] | None = None,
) -> str:
    """
    Truncate long observation text to preserve context window.

    If the text exceeds max_length, keeps the first and last portions
    with a warning message in between.

    Args:
        text: The observation text to potentially truncate
        max_length: Maximum allowed length before truncation
        action_str: Optional string representation of the action (for pattern matching)
        no_truncate_patterns: Optional list of patterns - if action contains any, skip truncation

    Returns:
        Original text if under limit, or truncated text with warning
    """
    if len(text) <= max_length:
        return text
    
    # Skip truncation if action matches any no-truncate pattern
    if should_skip_truncation(action_str, no_truncate_patterns):
        return text

    elided_count = len(text) - TRUNCATE_HEAD_CHARS - TRUNCATE_TAIL_CHARS

    output_head = text[:TRUNCATE_HEAD_CHARS]
    output_tail = text[-TRUNCATE_TAIL_CHARS:]

    warning_message = (
        f"\n\n[WARNING: Output truncated. Showing first {TRUNCATE_HEAD_CHARS} and "
        f"last {TRUNCATE_TAIL_CHARS} characters. {elided_count} characters elided.]\n\n"
    )

    return output_head + warning_message + output_tail


class ParseResultType(Enum):
    """Type of parse result."""
    ACTION = "action"           # Valid action to execute
    TASK_COMPLETE = "task_complete"  # Task completion signal
    FORMAT_ERROR = "format_error"    # Parse failed, need to remind model


@dataclass
class ParsedAction:
    """Represents a parsed action from model output."""
    name: str
    arguments: dict

    def __repr__(self) -> str:
        return f"ParsedAction(name={self.name!r}, arguments={self.arguments})"


@dataclass
class ParseResult:
    """Result of parsing model response."""
    type: ParseResultType
    action: ParsedAction | None = None
    error_message: str | None = None

    @property
    def is_action(self) -> bool:
        return self.type == ParseResultType.ACTION

    @property
    def is_task_complete(self) -> bool:
        return self.type == ParseResultType.TASK_COMPLETE

    @property
    def is_format_error(self) -> bool:
        return self.type == ParseResultType.FORMAT_ERROR


class ReActConverter:
    """
    Converter for ReAct-style prompts and responses.

    Handles:
    - Building system prompts with tool definitions
    - Parsing Action blocks from model responses
    - Formatting observations
    """

    ACTION_MARKER = "Action:"
    OBSERVATION_MARKER = "Observation:"

    def __init__(
        self,
        custom_system_template: str | None = None,
        prompt_builder: SystemPromptBuilder | None = None,
    ):
        """
        Initialize the converter.

        Args:
            custom_system_template: Optional custom system prompt template.
                Must contain {tool_definitions} placeholder.
            prompt_builder: Optional SystemPromptBuilder for sectioned prompts.
                If provided, takes precedence over custom_system_template.
        """
        self.system_template = custom_system_template or REACT_SYSTEM_TEMPLATE
        self.prompt_builder = prompt_builder

    def build_system_prompt(self, tools: list[Tool], extra_instructions: str = "") -> str:
        """
        Build the system prompt with tool definitions.

        Args:
            tools: List of available tools
            extra_instructions: Additional instructions to prepend

        Returns:
            Complete system prompt string
        """
        # Use prompt_builder if available
        if self.prompt_builder is not None:
            system_prompt = self.prompt_builder.build(tools=tools)
            if extra_instructions:
                system_prompt = f"{extra_instructions}\n\n{system_prompt}"
            return system_prompt

        # Legacy template-based approach
        tool_definitions = self._format_tool_definitions(tools)
        if self.system_template is REACT_SYSTEM_TEMPLATE:
            system_prompt = self.system_template.format(tool_definitions=tool_definitions)
        else:
            system_prompt = self.system_template.replace("{tool_definitions}", tool_definitions)

        if extra_instructions:
            system_prompt = f"{extra_instructions}\n\n{system_prompt}"

        return system_prompt

    def _format_tool_definitions(self, tools: list[Tool]) -> str:
        """Format tool definitions for the system prompt."""
        lines = []
        for tool in tools:
            params_schema = tool.get_params_schema()
            params_str = json.dumps(params_schema, indent=2)
            lines.append(f"- {tool.name}: {tool.description}")
            lines.append(f"    Parameters: {params_str}")
        return "\n".join(lines)

    def parse_response(self, response: str) -> ParseResult:
        """
        Parse a model response for actions or task completion signal.

        Args:
            response: The model's response text

        Returns:
            ParseResult indicating action, task completion, or format error
        """
        # DeepSeek and some OpenAI-compatible gateways may emit their native
        # DSML tool-call envelope instead of the text Action format requested
        # by the prompt. The control-token bytes can be rendered as replacement
        # characters on Windows, but the stable DSML/argument markers remain.
        dsml_action = self._parse_dsml_action(response)
        if dsml_action is not None:
            return ParseResult(type=ParseResultType.ACTION, action=dsml_action)

        # A provider may wrap an execution request as
        # <DSML...eval_execute><DSML...parameter name="code">...</parameter>.
        # This is the parameter-only variant of the DSML protocol.
        eval_action = self._parse_dsml_eval_execute(response)
        if eval_action is not None:
            return ParseResult(type=ParseResultType.ACTION, action=eval_action)

        # Be tolerant of Markdown-fenced tool requests. Some gateways strip
        # the ReAct markers while preserving the language fence.
        fenced = re.search(r"```\s*(python|bash|sh|json)\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
        if fenced is not None:
            tool_name = fenced.group(1).lower()
            if tool_name == "sh":
                tool_name = "bash"
            if tool_name == "json":
                try:
                    action_data = json.loads(fenced.group(2).strip())
                except json.JSONDecodeError:
                    action_data = None
                if isinstance(action_data, dict) and isinstance(action_data.get("name"), str):
                    return ParseResult(
                        type=ParseResultType.ACTION,
                        action=ParsedAction(
                            name=action_data["name"],
                            arguments=action_data.get("arguments", {}),
                        ),
                    )
                return ParseResult(type=ParseResultType.FORMAT_ERROR, error_message=FORMAT_ERROR_TEMPLATE)
            return ParseResult(
                type=ParseResultType.ACTION,
                action=ParsedAction(
                    name=tool_name,
                    arguments={"code" if tool_name == "python" else "command": fenced.group(2).strip()},
                ),
            )

        # If there's no tool Action block, treat as a direct final answer.  This
        # includes a bare TASK_COMPLETE signal, but deliberately happens after
        # action-envelope parsing: a response that contains both an executable
        # Action and ACTION: TASK_COMPLETE must run the Action first.
        # This keeps the agent usable for tasks that don't require tools and
        # matches the expectations in tests (a plain answer ends the run).
        if self.ACTION_MARKER not in response:
            return ParseResult(type=ParseResultType.TASK_COMPLETE)

        # Find the Action block
        action_start = response.find(self.ACTION_MARKER)
        action_text = response[action_start + len(self.ACTION_MARKER):].strip()

        # Find JSON object - handle nested braces
        brace_count = 0
        json_start = None
        json_end = None

        for i, char in enumerate(action_text):
            if char == '{':
                if brace_count == 0:
                    json_start = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and json_start is not None:
                    json_end = i + 1
                    break

        if json_start is None or json_end is None:
            return ParseResult(
                type=ParseResultType.FORMAT_ERROR,
                error_message=FORMAT_ERROR_TEMPLATE,
            )

        json_str = action_text[json_start:json_end]

        try:
            action_data = json.loads(json_str)
        except json.JSONDecodeError:
            # Try to fix common issues
            try:
                # Replace single quotes with double quotes
                fixed = json_str.replace("'", '"')
                action_data = json.loads(fixed)
            except json.JSONDecodeError:
                return ParseResult(
                    type=ParseResultType.FORMAT_ERROR,
                    error_message=FORMAT_ERROR_TEMPLATE,
                )

        if "name" not in action_data:
            return ParseResult(
                type=ParseResultType.FORMAT_ERROR,
                error_message=FORMAT_ERROR_TEMPLATE,
            )

        return ParseResult(
            type=ParseResultType.ACTION,
            action=ParsedAction(
                name=action_data["name"],
                arguments=action_data.get("arguments", {}),
            ),
        )

    @staticmethod
    def _parse_dsml_action(response: str) -> ParsedAction | None:
        """Parse a provider-native DSML tool call when present."""
        if "DSML" not in response:
            return None

        # DeepSeek also emits an eval_action/eval_execute envelope with a
        # normal JSON tool call in its body.  It is not prefixed by the ReAct
        # ``Action:`` marker, so parse the embedded object before attempting
        # the tag-oriented variants below.
        decoder = json.JSONDecoder()
        for index, character in enumerate(response):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(response[index:])
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("name")
            arguments = candidate.get("arguments")
            if isinstance(name, str) and isinstance(arguments, dict):
                return ParsedAction(name=name, arguments=arguments)

        # Structured DSML form used by tool-call gateways:
        # <DSML...tool_calls><DSML...invoke name="bash">
        # <DSML...parameter name="command">...</parameter>
        invoke_match = re.search(r'invoke\s+name="([A-Za-z_][A-Za-z0-9_-]*)"', response, re.DOTALL)
        if invoke_match is not None:
            arguments: dict[str, str] = {}
            for parameter in re.finditer(
                r'parameter\s+name="([A-Za-z_][A-Za-z0-9_-]*)"[^>]*>(.*?)</[^>]*parameter>',
                response,
                re.DOTALL,
            ):
                arguments[parameter.group(1)] = parameter.group(2).strip()
            if arguments:
                return ParsedAction(name=invoke_match.group(1), arguments=arguments)

        # Eval-execute DSML form wraps raw tool input between a tool tag and
        # its closing tag, e.g. <DSML...bash>...</DSML...bash>.
        for tool_name in ("python", "bash"):
            open_match = re.search(
                rf"DSML.*?{tool_name}\s*>",
                response,
                re.DOTALL,
            )
            if open_match is None:
                continue
            body = response[open_match.end():]
            close_match = re.search(
                rf"</[^>]*DSML[^>]*{tool_name}\s*>",
                body,
                re.DOTALL,
            )
            if close_match is not None:
                body = body[:close_match.start()]
            body = body.strip()
            parameter_match = re.search(
                r'parameter\s+name="(?:code|command)"[^>]*>(.*?)</[^>]*parameter>',
                body,
                re.DOTALL,
            )
            if parameter_match is not None:
                body = parameter_match.group(1).strip()
            if body:
                return ParsedAction(
                    name=tool_name,
                    arguments={"code" if tool_name == "python" else "command": body},
                )

        if "<argument" not in response:
            return None
        name_match = re.search(
            r'DSML.*?"([A-Za-z_][A-Za-z0-9_-]*)"\s*>',
            response,
            re.DOTALL,
        )
        argument_match = re.search(
            r"<argument\d+>(.*?)</argument\d+>",
            response,
            re.DOTALL,
        )
        if name_match is None or argument_match is None:
            return None
        try:
            arguments = json.loads(argument_match.group(1).strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        return ParsedAction(name=name_match.group(1), arguments=arguments)

    @staticmethod
    def _parse_dsml_eval_execute(response: str) -> ParsedAction | None:
        """Parse DSML eval-execute calls whose tool name is implicit."""
        if "eval_execute" not in response:
            return None
        for parameter_name, tool_name, argument_name in (
            ("code", "python", "code"),
            ("command", "bash", "command"),
        ):
            match = re.search(
                rf'parameter\s+name="{parameter_name}"[^>]*>(.*?)</[^>]*parameter>',
                response,
                re.DOTALL,
            )
            if match is not None and match.group(1).strip():
                return ParsedAction(name=tool_name, arguments={argument_name: match.group(1).strip()})
        code_match = re.search(r"eval_code>(.*?)</[^>]*eval_code>", response, re.DOTALL)
        if code_match is not None and code_match.group(1).strip():
            return ParsedAction(name="python", arguments={"code": code_match.group(1).strip()})
        return None

    def parse_action(self, response: str) -> ParsedAction | None:
        """
        Legacy method for backward compatibility.
        Parse an Action block from model response.

        Args:
            response: The model's response text

        Returns:
            ParsedAction if found, None otherwise
        """
        result = self.parse_response(response)
        if result.is_action:
            return result.action
        return None

    def format_observation(self, result: str, truncate: bool = True) -> str:
        """
        Format a tool result as an observation.

        Args:
            result: The raw tool result
            truncate: Whether to truncate long outputs (default: True)

        Returns:
            Formatted observation string
        """
        if truncate:
            result = truncate_observation(result)
        return f"{self.OBSERVATION_MARKER} {result}"

    def format_action(self, name: str, arguments: dict) -> str:
        """Format an action for display."""
        return ACTION_TEMPLATE.format(
            name=name,
            arguments=json.dumps(arguments, indent=4, ensure_ascii=False),
        )

    def get_stop_sequences(self) -> list[str]:
        """Get stop sequences for the model to prevent hallucinating observations."""
        return [self.OBSERVATION_MARKER]

    def extract_final_answer(self, response: str) -> str:
        """
        Extract the final answer from a response.

        Returns the text content before the TASK_COMPLETE signal or Action block.
        """
        # Check for task complete signal first
        if TASK_COMPLETE_SIGNAL in response:
            signal_start = response.find(TASK_COMPLETE_SIGNAL)
            return response[:signal_start].strip()

        # Check for Action block
        if self.ACTION_MARKER in response:
            action_start = response.find(self.ACTION_MARKER)
            return response[:action_start].strip()

        return response.strip()
