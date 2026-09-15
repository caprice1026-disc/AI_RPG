"""HTTP境界で受け付けるプレイヤーTurn契約。"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import Field

from ai_rpg.contracts.common import Contract, InputText, NonNegativeInt


class TextInput(Contract):
    kind: Literal["text"]
    text: InputText


class ChoiceInput(Contract):
    kind: Literal["choice"]
    choice_id: UUID


PlayerContent: TypeAlias = Annotated[TextInput | ChoiceInput, Field(discriminator="kind")]


class PlayerTurnInput(Contract):
    request_id: UUID
    expected_state_version: NonNegativeInt
    actor_id: UUID
    content: PlayerContent
