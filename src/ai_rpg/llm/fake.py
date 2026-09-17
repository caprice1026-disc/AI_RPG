"""構造化出力adapterの下で使うscript式Fake transport。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from ai_rpg.llm.structured import LLMPurpose


@dataclass(frozen=True, slots=True)
class FakeCall:
    model_id: str
    purpose: LLMPurpose
    instruction: str
    input_data: str
    output_schema: dict[str, object]


class ScriptedFakeTransport:
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
        self.calls.append(
            FakeCall(model_id, purpose, instruction, input_data, output_schema)
        )
        outcome = self._script[self.request_count]
        self.request_count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DevelopmentFakeTransport:
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
        facts = actions[0].get("result", {}).get("facts", []) if actions else []
        narration = str(facts[0]) if facts else "判定結果が確定した。"
        return {"narration": narration, "choices": []}
