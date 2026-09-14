"""用途tierから設定済みmodel IDを解決する。"""

from enum import StrEnum


class ModelTier(StrEnum):
    """特定providerのモデル名から独立した用途区分。"""

    FAST = "fast"
    QUALITY = "quality"
    BACKGROUND = "background"


class ModelRouter:
    """起動時設定を用いてmodel IDを選択する。"""

    def __init__(self, models: dict[ModelTier, str]) -> None:
        if any(not model_id for model_id in models.values()):
            raise ValueError("model IDを空にはできません")
        self._models = models.copy()

    def resolve(self, tier: ModelTier) -> str:
        """tierに対応するmodel IDを返す。"""

        try:
            return self._models[tier]
        except KeyError as error:
            raise ValueError(f"model tierが未設定です: {tier}") from error
