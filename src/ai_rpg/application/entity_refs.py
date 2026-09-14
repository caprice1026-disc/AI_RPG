"""LLM用参照名をCanonical IDへ変換するApplication専用機構。"""

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from pydantic import TypeAdapter

from ai_rpg.contracts.common import Ref

_ref_adapter = TypeAdapter(Ref)


class EntityRefMap:
    """Turn内参照とUUIDの対応を保持し、LLM契約からUUIDを隔離する。"""

    __slots__ = ("__ids",)

    def __init__(self, ids: Mapping[str, UUID]) -> None:
        checked = {_ref_adapter.validate_python(ref): entity_id for ref, entity_id in ids.items()}
        self.__ids = MappingProxyType(checked)

    def resolve(self, ref: str) -> UUID:
        """許可済みの参照だけをCanonical IDへ解決する。"""

        checked_ref = _ref_adapter.validate_python(ref)
        try:
            return self.__ids[checked_ref]
        except KeyError as error:
            raise ValueError("許可されていないEntity参照です") from error
