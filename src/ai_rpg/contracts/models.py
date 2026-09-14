"""Pydantic v2で表現する信頼境界の型。"""

from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model

InputText = Annotated[str, Field(min_length=1, max_length=8000)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class Contract(BaseModel):
    """未定義フィールドと変更を拒否する契約の基底型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TextInput(Contract):
    """自由記述によるプレイヤー入力。"""

    kind: Literal["text"]
    text: InputText


class ChoiceInput(Contract):
    """保存済み選択肢によるプレイヤー入力。"""

    kind: Literal["choice"]
    choice_id: UUID


PlayerContent: TypeAlias = Annotated[TextInput | ChoiceInput, Field(discriminator="kind")]


class PlayerTurnInput(Contract):
    """HTTP境界で受け付けるターン入力。"""

    request_id: UUID
    expected_state_version: NonNegativeInt
    actor_id: UUID
    content: PlayerContent


class ContextFragment(Contract):
    """出所、信頼度、公開範囲を保つコンテキスト断片。"""

    source: ShortText
    trust_level: Literal["trusted", "derived", "untrusted"]
    access_scope: Literal["public", "actor_private", "gm_private"]
    content: InputText


class AttackIntent(Contract):
    """LLMが解決を要求する攻撃意図。"""

    kind: Literal["attack"]
    target_ref: Ref
    weapon_ref: Ref | None


class SkillCheckIntent(Contract):
    """LLMが解決を要求する技能判定意図。"""

    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_ref: Ref | None


class UseItemIntent(Contract):
    """LLMが解決を要求するアイテム使用意図。"""

    kind: Literal["use_item"]
    item_ref: Ref
    target_ref: Ref | None


ActionIntent: TypeAlias = Annotated[
    AttackIntent | SkillCheckIntent | UseItemIntent, Field(discriminator="kind")
]


class ChoiceDraft(Contract):
    """LLMが提案する表示用選択肢。"""

    label: ShortText


class NarrativeDraft(Contract):
    """状態変更権限を持たないNarrative出力。"""

    kind: Literal["narrative"]
    narration: Annotated[str, Field(min_length=1, max_length=12000)]
    choices: list[ChoiceDraft] = Field(default_factory=list, max_length=5)


class ActionPlan(Contract):
    """Mechanical出力の行動計画。"""

    kind: Literal["action_plan"]
    actions: list[ActionIntent] = Field(min_length=1)


class ResolutionRequired(Contract):
    """NarrativeからMechanicalへの型付き昇格要求。"""

    kind: Literal["resolution_required"]
    actions: list[ActionIntent] = Field(min_length=1)


class ClarificationRequired(Contract):
    """推測せずプレイヤーへ確認するための出力。"""

    kind: Literal["clarification_required"]
    question: ShortText


def make_decision_types(max_actions: int) -> tuple[TypeAdapter[Any], TypeAdapter[Any]]:
    """設定されたAction上限をJSON Schemaと実検証の両方へ反映する。"""

    if type(max_actions) is not int or not 1 <= max_actions <= 10:
        raise ValueError("max_actionsは1以上10以下の整数である必要があります")
    plan = create_model(
        f"ActionPlanLimit{max_actions}",
        __base__=ActionPlan,
        actions=(list[ActionIntent], Field(min_length=1, max_length=max_actions)),
    )
    escalation = create_model(
        f"ResolutionRequiredLimit{max_actions}",
        __base__=ResolutionRequired,
        actions=(list[ActionIntent], Field(min_length=1, max_length=max_actions)),
    )
    narrative_type = Annotated[
        NarrativeDraft | escalation | ClarificationRequired,  # type: ignore[valid-type]
        Field(discriminator="kind"),
    ]
    mechanical_type = Annotated[
        plan | ClarificationRequired,  # type: ignore[valid-type]
        Field(discriminator="kind"),
    ]
    return TypeAdapter(narrative_type), TypeAdapter(mechanical_type)


class Choice(Contract):
    """保存済みの公開選択肢。"""

    id: UUID
    label: ShortText


class TurnResponse(Contract):
    """クライアントへ返すターンの公開DTO。"""

    turn_id: UUID
    route: Literal["narrative", "mechanical"] | None
    resolution_status: Literal["pending", "resolving", "committed", "not_applied", "failed"]
    narration_status: Literal["pending", "generating", "completed", "fallback"]
    committed_state_version: NonNegativeInt | None = None
    narration: Annotated[str, Field(min_length=1, max_length=12000)] | None = None
    choices: list[Choice] = Field(default_factory=list)
