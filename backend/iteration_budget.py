"""Simple iteration budget for AgentCore."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IterationBudget:
    max_iterations: int = 20
    max_tool_calls: int = 50
    iterations: int = 0
    tool_calls: int = 0

    @property
    def exhausted(self) -> bool:
        return self.iterations >= self.max_iterations or self.tool_calls >= self.max_tool_calls

    def record_iteration(self) -> None:
        self.iterations += 1

    def record_tool_call(self) -> None:
        self.tool_calls += 1
