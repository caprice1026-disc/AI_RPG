"""SSEなどのtransportから独立した公開イベント契約。"""

from typing import Literal

from ai_rpg.contracts.common import Contract, PositiveInt
from ai_rpg.contracts.responses import TurnResponse


class PublicTurnEventPayload(Contract):
    turn: TurnResponse


class PublicEvent(Contract):
    id: PositiveInt
    type: Literal["turn.updated"]
    schema_version: Literal[1]
    payload: PublicTurnEventPayload
