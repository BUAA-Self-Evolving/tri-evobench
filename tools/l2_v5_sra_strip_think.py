"""Verbatim helper extracted from pinned SR-Agents, not its LLM client.

Source: src/sragents/llm.py at 277fd8d2bbd7d3b81a5cf4ffa6e87e18c7906e4f.
Original file SHA256: ed73576ec6ba4d4db366b9271569df16082b25055eda3d58fd22d03f2b41e9a4
Only stdlib re import added; definitions below are unchanged upstream bytes.
"""
import re

_THINK_CLOSED_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from an LLM response.

    Handles both well-formed `<think>...</think>answer` and truncation
    cases where generation is cut mid-thinking (unclosed tag).
    """
    if "<think>" not in text:
        return text
    text = _THINK_CLOSED_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    return text.lstrip()
