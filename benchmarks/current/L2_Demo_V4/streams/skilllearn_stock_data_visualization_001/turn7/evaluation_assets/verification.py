"""Shared result contract for package-local deterministic verifiers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifyResult:
    category: str
    score: float
    details: dict[str, object]
