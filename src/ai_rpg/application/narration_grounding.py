"""Mechanical描写を保存済み公開結果へ限定する最小検査。"""

import re
from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel

from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput

_NUMBER = re.compile(r"(?<![0-9.])[-+]?\d+(?:\.\d+)?(?![0-9.])")
_EXPLICIT_REF = re.compile(r"@([a-z][a-z0-9_]{0,63})")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class NarrationGroundingError(ValueError):
    """描写が保存済み公開結果にない具体的事実を追加した。"""


def _collect_numbers(value: object, numbers: set[str]) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, UUID):
        return
    if isinstance(value, int | float):
        numbers.add(str(value))
        return
    if isinstance(value, str):
        numbers.update(_NUMBER.findall(value))
        return
    if isinstance(value, BaseModel):
        _collect_numbers(value.model_dump(mode="python"), numbers)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not str(key).endswith("_id") and key not in {"ordinal"}:
                _collect_numbers(nested, numbers)
        return
    if isinstance(value, list | tuple):
        for nested in value:
            _collect_numbers(nested, numbers)


def validate_mechanical_narration(
    source: MechanicalNarrationInput,
    draft: MechanicalNarrationDraft,
) -> None:
    """未知の数値、明示ref、Canonical UUIDを含む描写を拒否する。"""

    allowed_numbers: set[str] = set()
    _collect_numbers(source.resolved_actions, allowed_numbers)
    _collect_numbers([fragment.content for fragment in source.public_state_after], allowed_numbers)

    output_text = "\n".join(
        [draft.narration, *(choice.label for choice in draft.choices)]
    )
    if _UUID.search(output_text):
        raise NarrationGroundingError("Canonical UUIDを描写へ出力できません")
    unknown_numbers = set(_NUMBER.findall(output_text)) - allowed_numbers
    if unknown_numbers:
        raise NarrationGroundingError("保存済み結果にない数値を描写へ追加できません")
    allowed_refs = {entity.ref for entity in source.allowed_entity_refs}
    unknown_refs = set(_EXPLICIT_REF.findall(output_text)) - allowed_refs
    if unknown_refs:
        raise NarrationGroundingError("未登録entity/item refを描写へ追加できません")
