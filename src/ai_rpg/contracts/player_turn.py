"""HTTP境界で受け付けるプレイヤーTurn契約。"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field

from ai_rpg.contracts.common import Contract, InputText, NonNegativeInt, Ref


class TextInput(Contract):
    kind: Literal["text"]
    text: InputText


class ChoiceInput(Contract):
    kind: Literal["choice"]
    choice_id: UUID


class ScenarioActionInput(Contract):
    kind: Literal["scenario_action"]
    action_ref: Ref


class ConfirmActionInput(Contract):
    kind: Literal["confirm_action"]
    proposal_id: UUID


PlayerContent: TypeAlias = Annotated[
    TextInput | ChoiceInput | ScenarioActionInput | ConfirmActionInput,
    Field(discriminator="kind"),
]


class PlayerTurnInput(Contract):
    request_id: UUID
    expected_state_version: NonNegativeInt
    actor_id: UUID
    content: PlayerContent
