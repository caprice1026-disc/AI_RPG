"""LLMの権限を型で制限する構造化出力契約。"""

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, TypeAdapter, create_model

from ai_rpg.contracts.common import Contract, NarrationText, Ref, ShortText


class AttackIntent(Contract):
    kind: Literal["attack"]
    target_ref: Ref
    weapon_ref: Ref | None


class SkillCheckIntent(Contract):
    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_ref: Ref | None


class UseItemIntent(Contract):
    kind: Literal["use_item"]
    item_ref: Ref
    target_ref: Ref | None


class ScenarioActionIntent(Contract):
    kind: Literal["scenario_action"]
    action_ref: Ref


ActionIntent: TypeAlias = Annotated[
    AttackIntent | SkillCheckIntent | UseItemIntent | ScenarioActionIntent,
    Field(discriminator="kind"),
]


class ChoiceDraft(Contract):
    label: ShortText


class NarrativeDraft(Contract):
    kind: Literal["narrative"]
    narration: NarrationText
    choices: list[ChoiceDraft] = Field(max_length=5)


class ActionPlan(Contract):
    kind: Literal["action_plan"]
    actions: list[ActionIntent] = Field(min_length=1)


class ResolutionRequired(Contract):
    kind: Literal["resolution_required"]
    actions: list[ActionIntent] = Field(min_length=1)


class ClarificationRequired(Contract):
    kind: Literal["clarification_required"]
    question: ShortText


def make_decision_output_types(max_actions: int = 3) -> tuple[Any, Any]:
    """保存済みAction上限を検証とJSON Schemaの双方へ埋め込む。"""

    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actionsは正の整数である必要があります")
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
        NarrativeDraft | escalation | ClarificationRequired,
        Field(discriminator="kind"),
    ]
    mechanical_type = Annotated[
        plan | ClarificationRequired,
        Field(discriminator="kind"),
    ]
    return narrative_type, mechanical_type


def make_decision_types(max_actions: int = 3) -> tuple[TypeAdapter[Any], TypeAdapter[Any]]:
    """既存の保存・Fake境界にもAgentと同じ出力型を使う。"""

    narrative_type, mechanical_type = make_decision_output_types(max_actions)
    return TypeAdapter(narrative_type), TypeAdapter(mechanical_type)


NarrativeDecision, MechanicalDecision = make_decision_types()
