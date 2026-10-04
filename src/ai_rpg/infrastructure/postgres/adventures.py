"""Atomic adventure setup and bounded public history; no development seed dependency."""

from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.ports.adventures import CharacterPreset, InvalidHistoryCursorError
from ai_rpg.application.ports.repositories import AuthorizationError, IdempotencyConflictError
from ai_rpg.contracts.adventures import (
    AdventureHistoryResponse,
    AdventureListResponse,
    AdventureSummary,
    CreateAdventureRequest,
    CreateAdventureResponse,
    HistoryItem,
)
from ai_rpg.infrastructure.postgres.models import (
    AdventureStartRequestModel,
    CampaignMemberModel,
    CampaignModel,
    EntityModel,
    MvpCharacterAbilityModel,
    MvpCharacterModel,
    MvpInventoryModel,
    MvpScenarioRunModel,
    MvpSceneEntityModel,
    MvpSceneSkillCheckModel,
    MvpSkillModifierModel,
    MvpWeaponModel,
    SceneModel,
    TurnChoiceModel,
    TurnModel,
)
from ai_rpg.infrastructure.postgres.repositories import PostgresTurnRepository
from ai_rpg.infrastructure.postgres.scenario_source import (
    decode_version,
    published_title,
    resolve_start,
)
from ai_rpg.infrastructure.postgres.story_models import StoryPlaytestRecordModel, StoryVersionModel
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioDefinition
from ai_rpg.scenarios.initialization import initialization_for
from ai_rpg.scenarios.models import InitialCharacter, InitialItem, SkillScenarioAction


async def _authorize(session: AsyncSession, principal_id: UUID, campaign_id: UUID) -> None:
    # The caller already holds the Campaign lock; keep membership stable through this read.
    member = await session.scalar(
        select(CampaignMemberModel.principal_id)
        .where(
            CampaignMemberModel.campaign_id == campaign_id,
            CampaignMemberModel.principal_id == principal_id,
            CampaignMemberModel.active.is_(True),
        )
        .with_for_update(read=True)
    )
    if member is None:
        raise AuthorizationError("Campaign access denied")


class PostgresAdventureStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        principal_id: UUID,
        request: CreateAdventureRequest,
        scenario: ScenarioDefinition,
        preset: CharacterPreset,
        *,
        story_version_id: UUID | None = None,
    ) -> CreateAdventureResponse:
        campaign_id, actor_id = uuid4(), uuid4()
        payload = request.model_dump(mode="json", exclude_none=True)
        async with self._sessions.begin() as session:
            # The unique key waits for a competing transaction to commit or roll back.
            # Deferred FKs let us claim the request before constructing any game state.
            inserted = await session.scalar(
                insert(AdventureStartRequestModel)
                .values(
                    principal_id=principal_id,
                    request_id=request.request_id,
                    input_payload=payload,
                    campaign_id=campaign_id,
                    actor_id=actor_id,
                )
                .on_conflict_do_nothing(index_elements=["principal_id", "request_id"])
                .returning(AdventureStartRequestModel.campaign_id)
            )
            if inserted is None:
                previous = (
                    await session.execute(
                        select(AdventureStartRequestModel).where(
                            AdventureStartRequestModel.principal_id == principal_id,
                            AdventureStartRequestModel.request_id == request.request_id,
                        )
                    )
                ).scalar_one()
                if previous.input_payload != payload:
                    raise IdempotencyConflictError("Different adventure start payload")
                await session.scalar(
                    select(CampaignModel.id)
                    .where(
                        CampaignModel.id == previous.campaign_id,
                    )
                    .with_for_update()
                )
                await _authorize(session, principal_id, previous.campaign_id)
                if story_version_id is not None:
                    previous_version = await session.scalar(
                        select(MvpScenarioRunModel.story_version_id)
                        .where(MvpScenarioRunModel.campaign_id == previous.campaign_id)
                    )
                    await resolve_start(
                        session, principal_id, request, lock=True,
                        replay_version_id=previous_version,
                    )
                return CreateAdventureResponse(
                    campaign_id=previous.campaign_id, actor_id=previous.actor_id
                )

            if story_version_id is not None:
                resolved = await resolve_start(session, principal_id, request, lock=True)
                if resolved.version_id != story_version_id:
                    raise IdempotencyConflictError("Story version changed during adventure start")
                scenario = resolved.definition
            initial = initialization_for(scenario)
            initial_entities: tuple[InitialCharacter | InitialItem, ...] = (
                *initial.characters, *initial.items,
            )
            await session.execute(
                insert(CampaignModel).values(id=campaign_id, ruleset_version=scenario.ruleset_ref)
            )
            await session.execute(
                insert(CampaignMemberModel).values(
                    campaign_id=campaign_id,
                    principal_id=principal_id,
                    role="player",
                )
            )
            entity_ids = {
                "hero": actor_id,
                **{entity.ref: uuid4() for entity in initial_entities},
            }
            await session.execute(
                insert(EntityModel),
                [
                    {
                        "id": actor_id,
                        "campaign_id": campaign_id,
                        "kind": "pc",
                        "controller_id": principal_id,
                        "ref": "hero",
                        "label": request.player_name,
                    },
                    *(
                        {
                            "id": entity_ids[entity.ref],
                            "campaign_id": campaign_id,
                            "kind": "npc" if isinstance(entity, InitialCharacter) else "item",
                            "controller_id": None,
                            "ref": entity.ref,
                            "label": entity.label,
                        }
                        for entity in initial_entities
                    ),
                ],
            )
            scene_ids = {scene.sequence: uuid4() for scene in scenario.scenes}
            await session.execute(
                insert(SceneModel),
                [
                    {
                        "id": scene_ids[scene.sequence],
                        "campaign_id": campaign_id,
                        "sequence": scene.sequence,
                        "status": "active" if scene.sequence == 1 else "planned",
                    }
                    for scene in scenario.scenes
                ],
            )
            await session.execute(
                insert(MvpCharacterModel),
                [
                    {
                        "campaign_id": campaign_id,
                        "entity_id": actor_id,
                        "current_hp": preset.summary.max_hp,
                        "max_hp": preset.summary.max_hp,
                        "defense": preset.defense,
                        "attack_bonus": preset.attack_bonus,
                    },
                    *(
                        {
                            "campaign_id": campaign_id,
                            "entity_id": entity_ids[character.ref],
                            "current_hp": character.max_hp,
                            "max_hp": character.max_hp,
                            "defense": character.defense,
                            "attack_bonus": character.attack_bonus,
                        }
                        for character in initial.characters
                    ),
                ],
            )
            if scenario.ruleset_ref == "mvp_v2":
                assert request.ability_points is not None
                assert request.specialty_skill is not None
                assert preset.summary.base_abilities is not None
                base = preset.summary.base_abilities
                points = request.ability_points
                await session.execute(
                    insert(MvpCharacterAbilityModel).values(
                        campaign_id=campaign_id,
                        character_id=actor_id,
                        **{
                            key: getattr(base, key) + getattr(points, key)
                            for key in ("strength", "agility", "insight", "presence")
                        },
                        specialty_skill=request.specialty_skill,
                    )
                )
            weapons = [
                {
                    "campaign_id": campaign_id,
                    "entity_id": entity_ids[item.ref],
                    "damage_expression": item.weapon.damage_expression,
                    "damage_bonus": item.weapon.damage_bonus,
                }
                for item in initial.items if item.weapon is not None
            ]
            if weapons:
                await session.execute(insert(MvpWeaponModel), weapons)
            inventory = [
                {
                    "campaign_id": campaign_id,
                    "owner_id": entity_ids[item.owner_ref],
                    "item_id": entity_ids[item.ref],
                    "quantity": item.quantity,
                    "equipped": item.equipped,
                }
                for item in initial.items if item.owner_ref is not None
            ]
            if inventory:
                await session.execute(insert(MvpInventoryModel), inventory)
            await session.execute(
                insert(MvpSkillModifierModel),
                [
                    {
                        "campaign_id": campaign_id,
                        "character_id": actor_id,
                        "skill_ref": ref,
                        "modifier": value,
                    }
                    for ref, value in (
                        ("perception", preset.perception),
                        ("persuasion", preset.persuasion),
                        ("stealth", preset.stealth),
                    )
                ],
            )
            await session.execute(
                insert(MvpSceneEntityModel),
                [
                    *(
                        {
                            "campaign_id": campaign_id,
                            "scene_id": scene_id,
                            "entity_id": actor_id,
                            "is_public": True,
                            "is_attack_reachable": False,
                        }
                        for scene_id in scene_ids.values()
                    ),
                    *(
                        {
                            "campaign_id": campaign_id,
                            "scene_id": scene_ids[next(
                                scene.sequence for scene in scenario.scenes
                                if scene.scene_ref == placement.scene_ref
                            )],
                            "entity_id": entity_ids[placement.entity_ref],
                            "is_public": placement.visibility == "public",
                            "is_attack_reachable": placement.attackable,
                        }
                        for placement in initial.placements
                        if placement.visibility != "author_only"
                    ),
                ],
            )
            checks = [
                {
                    "campaign_id": campaign_id,
                    "scene_id": scene_ids[scene.sequence],
                    "check_ref": action.action_ref,
                    "skill_ref": action.skill_ref,
                    "difficulty": action.check_ref,
                    "public_description": action.label,
                }
                for scene in scenario.scenes
                for action in scene.actions
                if isinstance(action, SkillScenarioAction)
            ]
            if checks:
                await session.execute(insert(MvpSceneSkillCheckModel), checks)
            await session.execute(
                insert(MvpScenarioRunModel).values(
                    campaign_id=campaign_id,
                    scenario_ref=scenario.scenario_ref,
                    scenario_version=scenario.version,
                    story_version_id=story_version_id,
                    status="active",
                )
            )
            if story_version_id is not None:
                version = await session.get(StoryVersionModel, story_version_id)
                if version is not None and version.kind == "playtest":
                    session.add(StoryPlaytestRecordModel(
                        campaign_id=campaign_id, story_version_id=story_version_id,
                        actor_id=principal_id, debug_modified=False, author_acknowledged=False,
                    ))
        return CreateAdventureResponse(campaign_id=campaign_id, actor_id=actor_id)

    async def list_owned(self, principal_id: UUID) -> AdventureListResponse:
        async with self._sessions.begin() as session:
            rows = (
                await session.execute(
                    select(
                        CampaignModel.id.label("campaign_id"),
                        EntityModel.id.label("actor_id"),
                        func.coalesce(EntityModel.label, "Player").label("player_name"),
                        MvpScenarioRunModel.scenario_ref,
                        MvpScenarioRunModel.scenario_version,
                        MvpScenarioRunModel.story_version_id,
                        MvpScenarioRunModel.status,
                        CampaignModel.state_version,
                        CampaignModel.created_at,
                    )
                    .join(CampaignMemberModel, CampaignMemberModel.campaign_id == CampaignModel.id)
                    .join(EntityModel, EntityModel.campaign_id == CampaignModel.id)
                    .join(MvpCharacterModel, MvpCharacterModel.entity_id == EntityModel.id)
                    .join(MvpScenarioRunModel, MvpScenarioRunModel.campaign_id == CampaignModel.id)
                    .where(
                        CampaignMemberModel.principal_id == principal_id,
                        CampaignMemberModel.active.is_(True),
                        CampaignModel.status == "active",
                        EntityModel.kind == "pc",
                        EntityModel.controller_id == principal_id,
                        EntityModel.archived_at.is_(None),
                    )
                    .order_by(
                        CampaignModel.created_at.desc(), CampaignModel.id.desc(), EntityModel.id
                    )
                )
            ).mappings()
            adventures = []
            for row in rows:
                values = dict(row)
                version_id = values.pop("story_version_id")
                if version_id is None:
                    definition = BUILTIN_SCENARIOS.get(row["scenario_ref"], row["scenario_version"])
                    title = definition.title
                else:
                    version = await session.get(StoryVersionModel, version_id)
                    assert version is not None
                    definition = decode_version(version)
                    title = published_title(version, definition)
                adventures.append(AdventureSummary(**values, title=title))
            return AdventureListResponse(adventures=adventures)

    async def history(
        self,
        principal_id: UUID,
        campaign_id: UUID,
        limit: int,
        before_turn_id: UUID | None,
    ) -> AdventureHistoryResponse:
        async with self._sessions.begin() as session:
            await session.scalar(
                select(CampaignModel.id)
                .where(
                    CampaignModel.id == campaign_id,
                )
                .with_for_update()
            )
            await _authorize(session, principal_id, campaign_id)
            query = (
                select(
                    TurnModel.id,
                    TurnModel.created_at,
                    func.coalesce(TurnModel.input_text, TurnChoiceModel.label).label(
                        "player_input"
                    ),
                )
                .outerjoin(TurnChoiceModel, TurnChoiceModel.id == TurnModel.selected_choice_id)
                .where(
                    TurnModel.campaign_id == campaign_id,
                )
            )
            if before_turn_id is not None:
                cursor = (
                    await session.execute(
                        select(TurnModel.created_at, TurnModel.id).where(
                            TurnModel.campaign_id == campaign_id,
                            TurnModel.id == before_turn_id,
                        )
                    )
                ).one_or_none()
                if cursor is None:
                    raise InvalidHistoryCursorError("Cursor is not in this campaign")
                query = query.where(
                    tuple_(TurnModel.created_at, TurnModel.id)
                    < tuple_(cursor.created_at, cursor.id)
                )
            rows = (
                await session.execute(
                    query.order_by(
                        TurnModel.created_at.desc(),
                        TurnModel.id.desc(),
                    ).limit(limit + 1)
                )
            ).all()
            page = rows[:limit]
            turns = PostgresTurnRepository(session)
            items = []
            for row in reversed(page):
                turn = await turns.get_response(campaign_id, row.id)
                assert turn is not None
                items.append(
                    HistoryItem(created_at=row.created_at, player_input=row.player_input, turn=turn)
                )
            return AdventureHistoryResponse(
                items=items,
                next_before_turn_id=page[-1].id if len(rows) > limit else None,
            )
