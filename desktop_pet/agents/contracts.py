"""Data contracts crossing the chat/Agent boundary; no model or UI imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class AgentValidationError(ValueError):
    """A request or non-secret setting has an invalid shape."""


class AgentError(RuntimeError):
    """Only a fixed, non-secret message may cross into the chat UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AgentRequest:
    task: str
    kind: Literal["tools", "computer", "agent"] = "tools"

    def validate(self) -> None:
        if (not isinstance(self.task, str) or not self.task.strip()
                or len(self.task) > 12_000
                or any(ord(char) < 32 and char not in "\n\t" for char in self.task)
                or "\x7f" in self.task):
            raise AgentValidationError("Agent 任务必须是 1～12000 字符的有效文本。")
        if not isinstance(self.kind, str) or self.kind not in ("tools", "computer", "agent"):
            raise AgentValidationError("Agent 任务类型必须为 tools、computer 或 agent。")

    @classmethod
    def from_value(cls, value) -> AgentRequest | None:
        """Null denotes ordinary chat. Everything else must be a strict object.

        Tool/command names, credentials, settings and paths cannot be supplied
        by the model's routing field. They belong to the host configuration.
        """
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {"task", "kind"}
                or value.get("kind") not in ("tools", "computer")):
            raise AgentValidationError("Agent 请求只能包含 task 和 kind。")
        result = cls(task=value["task"], kind=value["kind"])
        result.validate()
        return result


@dataclass(frozen=True)
class AgentResult:
    reply: str
    emotion: str = "neutral"
    kind: Literal["tools", "computer", "agent"] = "tools"
    backend: str = "deepseek-harness"
    status: str = "completed"
    artifacts: tuple[dict, ...] = ()
    acceptance: dict | None = None
