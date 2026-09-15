"""旧import pathを維持する互換モジュール。新規コードは責務別モジュールを使う。"""

from ai_rpg.contracts.common import *  # noqa: F403
from ai_rpg.contracts.context import *  # noqa: F403
from ai_rpg.contracts.llm_decisions import *  # noqa: F403
from ai_rpg.contracts.player_turn import *  # noqa: F403
from ai_rpg.contracts.responses import *  # noqa: F403
