"""Plain data and one provider port; no SDK types cross this boundary."""

from dataclasses import dataclass
from typing import Any, Protocol

from ai_rpg.contracts.stories import AuthoringDraft
from ai_rpg.contracts.story_jobs import GeneratedStoryOutput


@dataclass(frozen=True)
class AuthoringInput:
    kind: str
    snapshot: AuthoringDraft
    instructions: str
    outline: dict[str, Any] | None


@dataclass
class AuthoringUsage:
    """Partial accounting survives provider failure, timeout and cancellation."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    complete: bool = False
    actual_model: str | None = None


@dataclass(frozen=True)
class AuthoringResult:
    output: GeneratedStoryOutput
    usage: AuthoringUsage


class AuthoringGenerationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StoryAuthoringLLM(Protocol):
    def prepare(self, context: AuthoringInput, *, model_id: str, max_bytes: int) -> str:
        """Local-only preparation, before durable call reservation."""
        ...

    async def generate(
        self,
        prompt: str,
        *,
        model_id: str,
        max_output_tokens: int,
        max_total_tokens: int,
        usage: AuthoringUsage,
    ) -> AuthoringResult:
        """One physical call after reservation; update usage even on failure."""
        ...
