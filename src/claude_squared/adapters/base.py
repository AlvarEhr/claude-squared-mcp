"""Abstract adapter — implement per CLI backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from claude_squared.models import (
    CompactResult,
    ContextReport,
    CreateResult,
    PairSpec,
    SendResult,
)


class PairAdapter(ABC):
    """Abstraction over a CLI backend that supports session resume."""

    backend_name: str

    @abstractmethod
    def create(self, spec: PairSpec, initial_message: str | None = None) -> CreateResult:
        ...

    @abstractmethod
    def send(self, spec: PairSpec, message: str, *, model: str | None = None,
             effort: str | None = None, permission_mode: str | None = None,
             timeout_seconds: int = 300) -> SendResult:
        ...

    @abstractmethod
    def compact(self, spec: PairSpec, steering_prompt: str | None = None,
                timeout_seconds: int = 600) -> CompactResult:
        ...

    @abstractmethod
    def context(self, spec: PairSpec, timeout_seconds: int = 60) -> ContextReport:
        ...

    @abstractmethod
    def invoke_skill(self, spec: PairSpec, skill_name: str, args: str | None = None,
                     timeout_seconds: int = 300) -> SendResult:
        ...

    @abstractmethod
    def transcript_path(self, spec: PairSpec) -> Path | None:
        ...

    @abstractmethod
    def session_exists(self, spec: PairSpec) -> bool:
        ...
