"""Frozen text heuristics for historical builtin versions only."""

import re

from ai_rpg.scenarios.models import ScenarioDefinition


def _contradicts_chapel_lore(text: str, protected_terms: tuple[str, ...]) -> bool:
    # ponytail: narrow v3 chapel claims, not general semantic consistency;
    # grow the evaluated examples before replacing this with a broader policy.
    if not any(term in text for term in protected_terms):
        return False
    patterns = (
        r"(?:最奥の間|奥の部屋|祭壇).{0,25}(?:空|誰も.{0,5}(?:いな|おらず)|何も.{0,8}(?:ない|置かれていな)|品物も存在しない)",
        r"(?:聖印|依頼品).{0,20}(?:存在しない|消えた|奪われた|もうない)",
        r"(?:聖印|依頼品).{0,25}(?:広間|長椅子|記録庫|裏庭).{0,10}(?:ある|置かれ|隠され)",
        r"(?:広間|長椅子|記録庫|裏庭).{0,12}(?:聖印|依頼品).{0,12}(?:ある|置かれ|隠され)",
        r"(?:ゴブリン|守衛|見張り).{0,40}(?:いない|おらず|立ち去|見張りをやめ|理由を聞かず)",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _contradicts_lighthouse_lore(text: str) -> bool:
    # ponytail: only explicit journal relocation; add evaluated patterns if playtests find more.
    journal = r"(?:航海日誌|日誌)"
    other_place = r"(?:船着き場|舟小屋|外階段|岩礁)"
    return any(re.search(pattern, text) for pattern in (
        rf"{journal}(?:は|が).{{0,25}}{other_place}.{{0,12}}(?:ある|置かれ|隠され|見つか)",
        rf"{other_place}.{{0,25}}{journal}(?:は|が).{{0,12}}(?:ある|置かれ|隠され|見つか)",
    ))


def contradicts_legacy_lore(
    definition: ScenarioDefinition, text: str, resulting_flags: frozenset[str] | set[str],
) -> bool:
    world = definition.world
    if definition.schema_version != 1 or world is None:
        return False
    if (definition.scenario_ref, definition.version) == ("ruined_chapel", 3):
        return _contradicts_chapel_lore(text, world.protected_terms)
    if (definition.scenario_ref, definition.version) == ("mist_lighthouse", 1):
        return (
            world.goal_flag_ref not in resulting_flags and _contradicts_lighthouse_lore(text)
        )
    return False
