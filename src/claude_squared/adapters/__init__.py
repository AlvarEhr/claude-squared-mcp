"""Adapter implementations for different CLI backends."""

from claude_squared.adapters.base import PairAdapter
from claude_squared.adapters.claude import ClaudeAdapter

__all__ = ["PairAdapter", "ClaudeAdapter"]
