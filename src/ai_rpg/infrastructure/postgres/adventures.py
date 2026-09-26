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
from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioDefinition
from ai_rpg.scenarios.models import AttackScenarioAction, SkillScenarioAction


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
                return CreateAdventureResponse(
                    campaign_id=previous.campaign_id, actor_id=previous.actor_id
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
            target_id, weapon_id, potion_id = uuid4(), uuid4(), uuid4()
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
                    {
                        "id": target_id,
                        "campaign_id": campaign_id,
                        "kind": "npc",
                        "controller_id": None,
                        "ref": "goblin",
                        "label": "ゴブリン",
                    },
                    {
                        "id": weapon_id,
                        "campaign_id": campaign_id,
                        "kind": "item",
                        "controller_id": None,
                        "ref": "iron_sword",
                        "label": "鉄の剣",
                    },
                    {
                        "id": potion_id,
                        "campaign_id": campaign_id,
                        "kind": "item",
                        "controller_id": None,
                        "ref": "healing_potion",
                        "label": "回復ポーション",
                    },
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
                    {
                        "campaign_id": campaign_id,
                        "entity_id": target_id,
                        "current_hp": 10,
                        "max_hp": 10,
                        "defense": 11,
                        "attack_bonus": 1,
                    },
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
            await session.execute(
                insert(MvpWeaponModel).values(
                    campaign_id=campaign_id,
                    entity_id=weapon_id,
                    damage_expression="1d6",
                    damage_bonus=0,
                )
            )
            await session.execute(
                insert(MvpInventoryModel),
                [
                    {
                        "campaign_id": campaign_id,
                        "owner_id": actor_id,
                        "item_id": weapon_id,
                        "quantity": 1,
                        "equipped": True,
                    },
                    {
                        "campaign_id": campaign_id,
                        "owner_id": actor_id,
                        "item_id": potion_id,
                        "quantity": 2,
                        "equipped": False,
                    },
                ],
            )
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
                            "scene_id": scene_ids[scene.sequence],
                            "entity_id": target_id,
                            "is_public": True,
                            "is_attack_reachable": True,
                        }
                        for scene in scenario.scenes
                        if any(
                            isinstance(action, AttackScenarioAction)
                            and action.target_ref == "goblin"
                            for action in scene.actions
                        )
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
                    status="active",
                )
            )
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
            return AdventureListResponse(
                adventures=[
                    AdventureSummary(
                        **row,
                        title=BUILTIN_SCENARIOS.get(
                            row["scenario_ref"],
                            row["scenario_version"],
                        ).title,
                    )
                    for row in rows
                ]
            )

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
