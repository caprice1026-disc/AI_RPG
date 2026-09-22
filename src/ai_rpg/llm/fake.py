"""用途別portを実装する、外部通信なしの決定的Fake。"""

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from pydantic import TypeAdapter

from ai_rpg.application.ports.llm import IntentResult, NarrativeResult
from ai_rpg.contracts.context import MechanicalInput, NarrativeInput
from ai_rpg.contracts.llm_decisions import make_decision_types
from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput
from ai_rpg.llm.prompts import (
    INTENT_INSTRUCTIONS,
    NARRATION_INSTRUCTIONS,
    NARRATIVE_INSTRUCTIONS,
)

LLMPurpose: TypeAlias = Literal["intent", "narrative", "result_narration"]


class _FakeLLM(ABC):
    async def generate_narrative(
        self, context: NarrativeInput, *, model_id: str
    ) -> NarrativeResult:
        adapter, _ = make_decision_types(context.output_limits.max_actions)
        raw = await self.request(
            model_id,
            "narrative",
            NARRATIVE_INSTRUCTIONS,
            context.model_dump_json(),
            adapter.json_schema(),
        )
        return cast(NarrativeResult, adapter.validate_python(raw))

    async def extract_intent(self, context: MechanicalInput, *, model_id: str) -> IntentResult:
        _, adapter = make_decision_types(context.output_limits.max_actions)
        raw = await self.request(
            model_id,
            "intent",
            INTENT_INSTRUCTIONS,
            context.model_dump_json(),
            adapter.json_schema(),
        )
        return cast(IntentResult, adapter.validate_python(raw))

    async def narrate_result(
        self, context: MechanicalNarrationInput, *, model_id: str
    ) -> MechanicalNarrationDraft:
        adapter = TypeAdapter(MechanicalNarrationDraft)
        raw = await self.request(
            model_id,
            "result_narration",
            NARRATION_INSTRUCTIONS,
            context.model_dump_json(),
            adapter.json_schema(),
        )
        return adapter.validate_python(raw)

    @abstractmethod
    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class FakeCall:
    model_id: str
    purpose: LLMPurpose
    instruction: str
    input_data: str
    output_schema: dict[str, object]


class ScriptedFakeLLM(_FakeLLM):
    """応答または例外を指定順に一度ずつ返す。"""

    def __init__(self, script: Sequence[object]) -> None:
        self._script = list(script)
        self.request_count = 0
        self.calls: list[FakeCall] = []

    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object:
        if self.request_count >= len(self._script):
            raise RuntimeError("Fake LLMのscriptを使い切りました")
        self.calls.append(FakeCall(model_id, purpose, instruction, input_data, output_schema))
        outcome = self._script[self.request_count]
        self.request_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DevelopmentFakeLLM(_FakeLLM):
    """別processの開発実行で同じ一往復を再現する決定的Fake。"""

    async def request(
        self,
        model_id: str,
        purpose: LLMPurpose,
        instruction: str,
        input_data: str,
        output_schema: dict[str, object],
    ) -> object:
        if purpose == "intent":
            payload = json.loads(input_data)
            player_text = str(payload.get("player_text", ""))
            if ("ポーション" in player_text or "回復" in player_text) and "use_item" in payload.get(
                "supported_action_types", []
            ):
                return {
                    "kind": "action_plan",
                    "actions": [
                        {"kind": "use_item", "item_ref": "healing_potion", "target_ref": None}
                    ],
                }
            scene_view = payload.get("scene_view")
            available_actions: set[str] | None = None
            if isinstance(scene_view, dict):
                content = scene_view.get("content")
                if isinstance(content, str):
                    try:
                        scene_context = json.loads(content)
                    except json.JSONDecodeError:
                        scene_context = None
                    if isinstance(scene_context, dict) and isinstance(
                        scene_context.get("available_actions"), list
                    ):
                        available_actions = {
                            str(action["action_ref"])
                            for action in scene_context["available_actions"]
                            if isinstance(action, dict) and "action_ref" in action
                        }
            if available_actions is not None:
                candidates = (
                    (
                        "enter_chapel",
                        ("入る",),
                        {"kind": "scenario_action", "action_ref": "enter_chapel"},
                    ),
                    (
                        "search_hall",
                        ("調べ", "探索"),
                        {
                            "kind": "skill_check",
                            "skill_ref": "perception",
                            "objective": "広間を調べる",
                            "target_ref": None,
                        },
                    ),
                    (
                        "negotiate_guard",
                        ("交渉",),
                        {
                            "kind": "skill_check",
                            "skill_ref": "persuasion",
                            "objective": "守衛と交渉する",
                            "target_ref": None,
                        },
                    ),
                    (
                        "sneak_to_relic",
                        ("隠れる", "隠密", "忍び寄"),
                        {
                            "kind": "skill_check",
                            "skill_ref": "stealth",
                            "objective": "聖印へ忍び寄る",
                            "target_ref": None,
                        },
                    ),
                    (
                        "defeat_guard",
                        ("攻撃", "守衛を倒す"),
                        {
                            "kind": "attack",
                            "target_ref": "goblin",
                            "weapon_ref": "iron_sword",
                        },
                    ),
                    (
                        "retreat",
                        ("撤退",),
                        {"kind": "scenario_action", "action_ref": "retreat"},
                    ),
                )
                for action_ref, markers, intent in candidates:
                    if action_ref in available_actions and any(
                        marker in player_text for marker in markers
                    ):
                        return {"kind": "action_plan", "actions": [intent]}
                return {
                    "kind": "clarification_required",
                    "question": "現在の場面で可能な行動を指定してください。",
                }
            if "攻撃" in player_text:
                return {
                    "kind": "action_plan",
                    "actions": [
                        {
                            "kind": "attack",
                            "target_ref": "goblin",
                            "weapon_ref": "iron_sword",
                        }
                    ],
                }
            if "ポーション" in player_text or "回復" in player_text:
                return {
                    "kind": "action_plan",
                    "actions": [
                        {
                            "kind": "use_item",
                            "item_ref": "healing_potion",
                            "target_ref": None,
                        }
                    ],
                }
            return {
                "kind": "action_plan",
                "actions": [
                    {
                        "kind": "skill_check",
                        "skill_ref": "perception",
                        "objective": "周囲の痕跡を見つける",
                        "target_ref": None,
                    }
                ],
            }
        if purpose == "narrative":
            return {
                "kind": "narrative",
                "narration": "静かな時間が流れている。",
                "choices": [],
            }
        payload = json.loads(input_data)
        actions = payload.get("resolved_actions", [])
        facts = [
            str(fact) for action in actions for fact in action.get("result", {}).get("facts", [])
        ]
        facts.extend(
            "敵の反撃: " + str(fact)
            for reaction in payload.get("enemy_reactions", [])
            for fact in reaction.get("result", {}).get("facts", [])
        )
        facts.extend(
            str(fragment["content"])
            for fragment in payload.get("public_state_after", [])
            if str(fragment.get("source", "")).startswith("scenario_")
        )
        narration = " / ".join(facts) if facts else "判定結果が確定した。"
        return {"narration": narration, "choices": []}
