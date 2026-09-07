"""Adapter implementations for different CLI backends."""

from claude_squared.adapters.base import PairAdapter
from claude_squared.adapters.claude import ClaudeAdapter
from claude_squared.adapters.codex import CodexAdapter

__all__ = ["PairAdapter", "ClaudeAdapter", "CodexAdapter"]
