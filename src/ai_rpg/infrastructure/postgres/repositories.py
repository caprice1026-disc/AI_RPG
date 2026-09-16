"""SQLAlchemy async sessionを用いるPostgreSQL Repository実装。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import update as sql_update
from sqlalchemy.sql.selectable import FromClause

from ai_rpg.application.ports.repositories import (
    AuthorizationError,
    CanonicalRepository,
    CanonicalSnapshot,
    ChoiceDraft,
    ChoiceNotAvailableError,
    CommitBundle,
    IdempotencyConflictError,
    InvalidCommitBundleError,
    Lease,
    LLMCallRepository,
    LLMPhase,
    NarrationLease,
    NarrationRepository,
    StateVersionConflictError,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
)
from ai_rpg.application.resolution import (
    CharacterHpMutation,
    InventoryQuantityMutation,
    TurnCommitContext,
    project_resolution,
)
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.contracts.responses import TurnRecovery
from ai_rpg.domain.commands import AttackCommand, UseItemCommand
from ai_rpg.domain.events import NarrationGeneratedPayload
from ai_rpg.infrastructure.postgres.models import (
    CampaignMemberModel,
    CampaignModel,
    EntityModel,
    MvpCharacterModel,
    MvpInventoryModel,
    MvpSkillModifierModel,
    MvpWeaponModel,
    SceneModel,
    TurnChoiceModel,
    TurnModel,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def request_hash(
    schema_version: int,
    campaign_id: UUID,
    scene_id: UUID,
    principal_id: UUID,
    player_input: PlayerTurnInput,
) -> bytes:
    """契約で固定した値を正規JSON化しSHA-256 digestを返す。"""
    envelope = {
        "schema_version": schema_version,
        "campaign_id": str(campaign_id),
        "scene_id": str(scene_id),
        "principal_id": str(principal_id),
        "input": player_input.model_dump(mode="json"),
    }
    return hashlib.sha256(_json(envelope).encode("utf-8")).digest()


def _turn(row: RowMapping) -> TurnRow:
    return TurnRow(
        id=row["id"],
        campaign_id=row["campaign_id"],
        scene_id=row["scene_id"],
        principal_id=row["created_by"],
        request_id=row["request_id"],
        resolution_status=str(row["resolution_status"]),
        worker_epoch=int(row["worker_epoch"]),
    )


def _pending_response(turn: TurnRow) -> TurnResponse:
    return TurnResponse(
        turn_id=turn.id,
        route=None,
        resolution_status="pending",
        narration_status="pending",
        committed_state_version=None,
        narration=None,
        choices=[],
        action_results=[],
        recovery=TurnRecovery(fallback=False, reason=None),
    )


async def _validate_canonical_mutations(
    session: AsyncSession,
    campaign_id: UUID,
    mutations: tuple[CharacterHpMutation | InventoryQuantityMutation, ...],
) -> None:
    """Canonical行をlockし、Engineが示した保存前値と現在値を照合する。"""

    for mutation in mutations:
        if isinstance(mutation, CharacterHpMutation):
            current = (
                await session.execute(
                    text(
                        """
                        SELECT current_hp,max_hp
                        FROM mvp_characters
                        WHERE campaign_id=:c AND entity_id=:e
                        FOR UPDATE
                        """
                    ),
                    {"c": campaign_id, "e": mutation.entity_id},
                )
            ).one_or_none()
            if current is None:
                raise InvalidCommitBundleError("Canonical HP更新対象が存在しません")
            if int(current.current_hp) != mutation.hp_before:
                raise InvalidCommitBundleError("Canonical HPの保存前値が一致しません")
            max_hp = int(current.max_hp)
            if mutation.max_hp is not None and mutation.max_hp != max_hp:
                raise InvalidCommitBundleError("Canonical max_hpがEngine結果と一致しません")
            if not 0 <= mutation.hp_after <= max_hp:
                raise InvalidCommitBundleError("Canonical HP更新値が不正です")
        else:
            current = (
                await session.execute(
                    text(
                        """
                        SELECT quantity
                        FROM mvp_inventory
                        WHERE campaign_id=:c AND owner_id=:o AND item_id=:i
                        FOR UPDATE
                        """
                    ),
                    {
                        "c": campaign_id,
                        "o": mutation.owner_id,
                        "i": mutation.item_id,
                    },
                )
            ).one_or_none()
            if current is None:
                raise InvalidCommitBundleError("Canonical在庫更新対象が存在しません")
            if int(current.quantity) != mutation.quantity_before:
                raise InvalidCommitBundleError("Canonical在庫の保存前値が一致しません")
            if mutation.quantity_after < 0:
                raise InvalidCommitBundleError("Canonical在庫更新値が不正です")


class PostgresTurnRepository:
    def __init__(
        self,
        session: AsyncSession,
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._event_id_factory = event_id_factory

    async def add(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        max_actions: int,
        llm_call_budget: int = 3,
    ) -> TurnResponse:
        """既存Application API向けにactive Sceneへpending Turnを追加する。"""
        if not await self._can_access_campaign(campaign_id, principal_id):
            raise AuthorizationError("Campaignを参照する権限がありません")
        existing = await self._request_row(campaign_id, principal_id, turn.request_id)
        if existing is not None:
            return await self._replay(existing, turn)

        row = await self.accept_pending(
            campaign_id,
            principal_id,
            turn,
            max_actions=max_actions,
            llm_call_budget=llm_call_budget,
        )
        if row is None:
            existing = await self._request_row(campaign_id, principal_id, turn.request_id)
            if existing is None:
                raise TurnInProgressError("別の未解決Turnが存在します")
            return await self._replay(existing, turn)
        return _pending_response(row)

    async def _can_access_campaign(self, campaign_id: UUID, principal_id: UUID) -> bool:
        result = await self._session.execute(
            select(
                exists().where(
                    CampaignMemberModel.campaign_id == campaign_id,
                    CampaignMemberModel.principal_id == principal_id,
                    CampaignMemberModel.active.is_(True),
                )
            ),
        )
        return bool(result.scalar_one())

    async def _replay(self, existing: RowMapping, turn: PlayerTurnInput) -> TurnResponse:
        existing = (
            await self._session.execute(
                text(
                    """
                    SELECT t.*,
                           COALESCE((
                               SELECT jsonb_agg(
                                   jsonb_build_object('id', c.id, 'label', c.label)
                                   ORDER BY c.ordinal
                               )
                               FROM turn_choices AS c
                               WHERE c.source_turn_id=t.id AND c.invalidated_at IS NULL
                           ), '[]'::jsonb) AS replay_choices,
                           COALESCE((
                               SELECT jsonb_agg(
                                   jsonb_build_object(
                                       'id', a.id,
                                       'ordinal', a.ordinal,
                                       'result', a.result
                                   )
                                   ORDER BY a.ordinal
                               )
                               FROM actions AS a
                               WHERE a.turn_id=t.id
                           ), '[]'::jsonb) AS replay_actions
                    FROM turns AS t
                    WHERE t.id=:turn
                    """
                ),
                {"turn": existing["id"]},
            )
        ).mappings().one()
        digest = request_hash(
            int(existing["input_schema_version"]),
            existing["campaign_id"],
            existing["scene_id"],
            existing["created_by"],
            turn,
        )
        if existing["input_payload"] != turn.model_dump(mode="json") or bytes(
            existing["request_hash"]
        ) != digest:
            raise IdempotencyConflictError("同じrequest_idに異なる入力は使用できません")

        return TurnResponse.model_validate(
            {
                "turn_id": existing["id"],
                "route": existing["route"],
                "resolution_status": existing["resolution_status"],
                "narration_status": existing["narration_status"],
                "committed_state_version": existing["committed_state_version"],
                "narration": existing["narration"],
                "choices": existing["replay_choices"],
                "action_results": [
                    {
                        "action_id": row["id"],
                        "ordinal": row["ordinal"],
                        "result": row["result"],
                    }
                    for row in existing["replay_actions"]
                ],
                "recovery": {
                    "fallback": existing["narration_status"] == "fallback",
                    "reason": existing["recovery_reason"],
                },
            }
        )

    async def _request_row(
        self, campaign_id: UUID, principal_id: UUID, request_id: UUID
    ) -> RowMapping | None:
        result = await self._session.execute(
            select(TurnModel.__table__).where(
                TurnModel.campaign_id == campaign_id,
                TurnModel.created_by == principal_id,
                TurnModel.request_id == request_id,
            )
        )
        return result.mappings().one_or_none()

    async def find_by_request_id(
        self, campaign_id: UUID, principal_id: UUID, request_id: UUID
    ) -> TurnRow | None:
        row = await self._request_row(campaign_id, principal_id, request_id)
        return None if row is None else _turn(row)

    async def accept_pending(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        *,
        max_actions: int,
        llm_call_budget: int = 3,
    ) -> TurnRow | None:
        # 全Canonical更新経路と同じくCampaignを最初にロックする。
        campaign = (
            await self._session.execute(
                select(CampaignModel.state_version, CampaignModel.status)
                .where(CampaignModel.id == campaign_id)
                .with_for_update()
            )
        ).mappings().one()
        if not await self._can_access_campaign(campaign_id, principal_id):
            raise AuthorizationError("Campaignを参照する権限がありません")
        if await self._request_row(campaign_id, principal_id, turn.request_id) is not None:
            return None
        if campaign["status"] != "active":
            raise AuthorizationError("停止中のCampaignへTurnは追加できません")
        actor_allowed = await self._session.execute(
            select(
                exists().where(
                    EntityModel.campaign_id == campaign_id,
                    EntityModel.id == turn.actor_id,
                    EntityModel.kind.in_(("pc", "npc")),
                    EntityModel.controller_id == principal_id,
                    EntityModel.archived_at.is_(None),
                    CampaignMemberModel.campaign_id == EntityModel.campaign_id,
                    CampaignMemberModel.principal_id == principal_id,
                    CampaignMemberModel.active.is_(True),
                )
            )
        )
        if not actor_allowed.scalar_one():
            raise AuthorizationError("actorを操作する権限がありません")
        open_turn = await self._session.execute(
            select(TurnModel.id).where(
                TurnModel.campaign_id == campaign_id,
                or_(
                    TurnModel.resolution_status.in_(("pending", "resolving")),
                    TurnModel.narration_status.in_(("pending", "generating")),
                ),
            )
        )
        if open_turn.scalar_one_or_none() is not None:
            return None
        scene_id = (
            await self._session.execute(
                select(SceneModel.id).where(
                    SceneModel.campaign_id == campaign_id,
                    SceneModel.status == "active",
                )
            )
        ).scalar_one()
        current_version = int(campaign["state_version"])
        if turn.expected_state_version != current_version:
            raise StateVersionConflictError("Campaignのstate versionが更新されています")
        content = turn.content.model_dump(mode="json")
        if turn.content.kind == "choice":
            available_choice = await self._session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM turn_choices AS c
                        JOIN turns AS source ON source.id=c.source_turn_id
                        WHERE c.id=:choice
                          AND c.campaign_id=:c
                          AND c.scene_id=:s
                          AND c.actor_id=:a
                          AND c.state_version=:v
                          AND c.invalidated_at IS NULL
                          AND source.campaign_id=:c
                          AND source.scene_id=:s
                          AND source.actor_id=:a
                          AND source.resolution_status='committed'
                          AND source.narration_status IN ('completed','fallback')
                    )
                    """
                ),
                {
                    "choice": turn.content.choice_id,
                    "c": campaign_id,
                    "s": scene_id,
                    "a": turn.actor_id,
                    "v": current_version,
                },
            )
            if not available_choice.scalar_one():
                raise ChoiceNotAvailableError("指定Choiceは現在利用できません")
        await self._session.execute(
            sql_update(TurnChoiceModel)
            .where(
                TurnChoiceModel.campaign_id == campaign_id,
                TurnChoiceModel.actor_id == turn.actor_id,
                TurnChoiceModel.invalidated_at.is_(None),
            )
            .values(invalidated_at=text("now()"))
        )
        params = {
            "id": uuid4(),
            "campaign_id": campaign_id,
            "scene_id": scene_id,
            "created_by": principal_id,
            "request_id": turn.request_id,
            "actor_id": turn.actor_id,
            "input_payload": turn.model_dump(mode="json"),
            "request_hash": request_hash(1, campaign_id, scene_id, principal_id, turn),
            "input_kind": content["kind"],
            "input_text": content.get("text"),
            "selected_choice_id": content.get("choice_id"),
            "expected_state_version": turn.expected_state_version,
            "max_actions": max_actions,
            "llm_call_budget": llm_call_budget,
        }
        result = await self._session.execute(
            insert(TurnModel)
            .on_conflict_do_nothing()
            .returning(*TurnModel.__table__.c),
            params,
        )
        row = result.mappings().one_or_none()
        return None if row is None else _turn(row)

    async def acquire_lease(
        self,
        turn_id: UUID,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> Lease | None:
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET resolution_status='resolving',
                    worker_epoch=worker_epoch+1,
                    lease_until=now()+make_interval(secs=>:lease_seconds),
                    resolution_attempt_count=resolution_attempt_count+1,
                    resolution_started_at=COALESCE(resolution_started_at,now()),
                    resolution_deadline=COALESCE(
                        resolution_deadline,
                        now()+make_interval(secs=>:deadline_seconds)
                    )
                WHERE id=(
                    SELECT id
                    FROM turns
                    WHERE id=:id
                      AND resolution_status IN ('pending','resolving')
                      AND (resolution_status='pending' OR lease_until<now())
                      AND resolution_attempt_count<:max_attempts
                      AND (resolution_deadline IS NULL OR resolution_deadline>now())
                      AND (
                          resolution_next_attempt_at IS NULL
                          OR resolution_next_attempt_at<=now()
                      )
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """
            ),
            {
                "id": turn_id,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
                "deadline_seconds": deadline_seconds,
            },
        )
        row = result.mappings().one_or_none()
        return None if row is None else Lease(_turn(row), row["lease_until"])

    async def commit_resolution(self, bundle: CommitBundle) -> int:
        # デッドロックを避ける不変順序: Campaign、Turn。
        campaign = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT state_version,event_sequence,ruleset_version
                        FROM campaigns
                        WHERE id=:c
                        FOR UPDATE
                        """
                    ),
                    {"c": bundle.campaign_id},
                )
            )
            .mappings()
            .one()
        )
        turn = (
            (
                await self._session.execute(
                    text("SELECT * FROM turns WHERE id=:t AND campaign_id=:c FOR UPDATE"),
                    {"t": bundle.turn_id, "c": bundle.campaign_id},
                )
            )
            .mappings()
            .one()
        )
        if turn["resolution_status"] == "committed":
            return int(turn["committed_state_version"])
        if (
            turn["resolution_status"] != "resolving"
            or int(turn["worker_epoch"]) != bundle.worker_epoch
            or turn["lease_until"] <= datetime.now(UTC)
            or int(campaign["state_version"]) != bundle.base_state_version
        ):
            raise RuntimeError("worker leaseまたはCanonical versionが無効です")
        first = int(campaign["event_sequence"]) + 1
        projection = project_resolution(
            bundle,
            TurnCommitContext(
                scene_id=turn["scene_id"],
                actor_id=turn["actor_id"],
                max_actions=int(turn["max_actions"]),
            ),
            first_event_sequence=first,
            event_id_factory=self._event_id_factory,
        )
        await _validate_canonical_mutations(
            self._session,
            bundle.campaign_id,
            projection.canonical_mutations,
        )
        for mutation in projection.canonical_mutations:
            if isinstance(mutation, CharacterHpMutation):
                updated = await self._session.execute(
                    text(
                        """
                        UPDATE mvp_characters
                        SET current_hp=:hp
                        WHERE campaign_id=:c AND entity_id=:e
                        RETURNING entity_id
                        """
                    ),
                    {
                        "hp": mutation.hp_after,
                        "c": bundle.campaign_id,
                        "e": mutation.entity_id,
                    },
                )
            else:
                updated = await self._session.execute(
                    text(
                        """
                        UPDATE mvp_inventory
                        SET quantity=:quantity
                        WHERE campaign_id=:c AND owner_id=:o AND item_id=:i
                        RETURNING item_id
                        """
                    ),
                    {
                        "quantity": mutation.quantity_after,
                        "c": bundle.campaign_id,
                        "o": mutation.owner_id,
                        "i": mutation.item_id,
                    },
                )
            if updated.scalar_one_or_none() is None:
                raise InvalidCommitBundleError("Canonical更新対象が消失しました")
        version = projection.committed_state_version
        await self._session.execute(
            text(
                "UPDATE campaigns SET state_version=:v,event_sequence=event_sequence+:n WHERE id=:c"
            ),
            {"v": version, "n": len(projection.events), "c": bundle.campaign_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE turns
                SET resolution_status='committed',route='mechanical',
                    committed_state_version=:v,committed_at=now(),
                    narration_input=CAST(:ni AS jsonb)
                WHERE id=:t
                """
            ),
            {
                "v": version,
                "ni": _json(projection.narration_input.model_dump(mode="json")),
                "t": bundle.turn_id,
            },
        )
        for action in projection.actions:
            command = action.command
            item_id = (
                command.weapon_id
                if isinstance(command, AttackCommand)
                else command.item_id
                if isinstance(command, UseItemCommand)
                else None
            )
            await self._session.execute(
                text(
                    """
                    INSERT INTO actions(
                        id,campaign_id,turn_id,ordinal,actor_id,kind,
                        target_id,item_id,command,result,result_kind,ruleset_version
                    )
                    VALUES(
                        :id,
                        :c,
                        :t,
                        :o,
                        :actor,
                        :kind,
                        :target,
                        :item,
                        CAST(:command AS jsonb),
                        CAST(:result AS jsonb),
                        :rk,
                        :ruleset
                    )
                    """
                ),
                {
                    "id": command.action_id,
                    "c": bundle.campaign_id,
                    "t": bundle.turn_id,
                    "o": command.ordinal,
                    "actor": command.actor_id,
                    "kind": command.kind,
                    "target": command.target_id,
                    "item": item_id,
                    "command": _json(command.model_dump(mode="json")),
                    "result": _json(action.result.model_dump(mode="json")),
                    "rk": action.result.kind,
                    "ruleset": campaign["ruleset_version"],
                },
            )
        for event in projection.events:
            await self._session.execute(
                text(
                    """
                    INSERT INTO events(
                        id,campaign_id,scene_id,turn_id,action_id,
                        sequence,state_version,type,schema_version,payload
                    )
                    VALUES(
                        :id,
                        :c,
                        :s,
                        :t,
                        :a,
                        :seq,
                        :v,
                        :type,
                        1,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": event.id,
                    "c": bundle.campaign_id,
                    "s": bundle.scene_id,
                    "t": bundle.turn_id,
                    "a": event.action_id,
                    "seq": event.sequence,
                    "v": event.state_version,
                    "type": event.type,
                    "payload": _json(event.payload.model_dump(mode="json")),
                },
            )
        return version


class PostgresCanonicalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, campaign_id: UUID) -> CanonicalSnapshot:
        # 呼出側はこの短い読取transactionを終了してからLLMへ進む。
        campaign = (
            await self._session.execute(
                select(CampaignModel.state_version)
                .where(CampaignModel.id == campaign_id)
                .with_for_update()
            )
        ).one()

        async def rows(table: FromClause) -> tuple[Mapping[str, object], ...]:
            result = await self._session.execute(
                select(table).where(table.c.campaign_id == campaign_id)
            )
            return tuple(dict(row) for row in result.mappings())

        return CanonicalSnapshot(
            campaign_id,
            int(campaign[0]),
            await rows(MvpCharacterModel.__table__),
            await rows(MvpSkillModifierModel.__table__),
            await rows(MvpWeaponModel.__table__),
            await rows(MvpInventoryModel.__table__),
        )

    async def update_with_campaign_lock(
        self, campaign_id: UUID, update: Callable[[], object]
    ) -> int:
        row = (
            await self._session.execute(
                select(CampaignModel.state_version)
                .where(CampaignModel.id == campaign_id)
                .with_for_update()
            )
        ).one()
        value = update()
        if hasattr(value, "__await__"):
            await value
        version = int(row[0]) + 1
        await self._session.execute(
            sql_update(CampaignModel)
            .where(CampaignModel.id == campaign_id)
            .values(state_version=version)
        )
        return version


class PostgresLLMCallRepository:
    """provider呼出し直前のTurn共有予算をDBで予約する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(
        self,
        turn_id: UUID,
        *,
        phase: LLMPhase,
        worker_epoch: int,
    ) -> bool:
        ownership = {
            "resolution": (
                "resolution_status='resolving' "
                "AND worker_epoch=:epoch AND lease_until>now()"
            ),
            "narration": (
                "narration_status='generating' "
                "AND narration_worker_epoch=:epoch AND narration_lease_until>now()"
            ),
        }.get(phase)
        if ownership is None:
            raise ValueError(f"未対応のLLM phaseです: {phase}")
        result = await self._session.execute(
            text(
                f"""
                UPDATE turns
                SET llm_call_count=llm_call_count+1
                WHERE id=:turn
                  AND {ownership}
                  AND llm_call_count<llm_call_budget
                  AND (route IS DISTINCT FROM 'narrative' OR llm_call_count<1)
                RETURNING llm_call_count
                """
            ),
            {"turn": turn_id, "epoch": worker_epoch},
        )
        return result.scalar_one_or_none() is not None


class PostgresNarrationRepository:
    def __init__(
        self,
        session: AsyncSession,
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._event_id_factory = event_id_factory

    async def acquire_lease(
        self,
        turn_id: UUID,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> NarrationLease | None:
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET narration_status='generating',
                    narration_worker_epoch=narration_worker_epoch+1,
                    narration_lease_until=now()+make_interval(secs=>:lease_seconds),
                    narration_attempt_count=narration_attempt_count+1,
                    narration_started_at=COALESCE(narration_started_at,now()),
                    narration_deadline=COALESCE(
                        narration_deadline,
                        now()+make_interval(secs=>:deadline_seconds)
                    )
                WHERE id=(
                    SELECT id
                    FROM turns
                    WHERE id=:id
                      AND resolution_status IN ('committed','not_applied','failed')
                      AND narration_status IN ('pending','generating')
                      AND (
                          narration_status='pending'
                          OR narration_lease_until<now()
                      )
                      AND narration_attempt_count<:max_attempts
                      AND (narration_deadline IS NULL OR narration_deadline>now())
                      AND (
                          narration_next_attempt_at IS NULL
                          OR narration_next_attempt_at<=now()
                      )
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id,campaign_id,narration_worker_epoch,narration_lease_until
                """
            ),
            {
                "id": turn_id,
                "lease_seconds": lease_seconds,
                "max_attempts": max_attempts,
                "deadline_seconds": deadline_seconds,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return NarrationLease(
            turn_id=row["id"],
            campaign_id=row["campaign_id"],
            worker_epoch=int(row["narration_worker_epoch"]),
            lease_until=row["narration_lease_until"],
        )

    async def save_conditionally(
        self,
        campaign_id: UUID,
        turn_id: UUID,
        worker_epoch: int,
        narration: str,
        choices: Sequence[ChoiceDraft],
        *,
        fallback_reason: str | None = None,
    ) -> bool:
        # Campaign→Turnのロック順を確定処理と統一する。
        campaign = (
            await self._session.execute(
                text(
                    "SELECT state_version,event_sequence FROM campaigns "
                    "WHERE id=:c FOR UPDATE"
                ),
                {"c": campaign_id},
            )
        ).mappings().one()
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET narration_status=:status,
                    narration=:n,
                    recovery_reason=:reason,
                    narration_lease_until=NULL
                WHERE id=:t
                  AND campaign_id=:c
                  AND narration_worker_epoch=:epoch
                  AND narration_status='generating'
                  AND narration_lease_until>now()
                RETURNING scene_id,actor_id,committed_state_version
                """
            ),
            {
                "status": "fallback" if fallback_reason else "completed",
                "n": narration,
                "reason": fallback_reason,
                "t": turn_id,
                "c": campaign_id,
                "epoch": worker_epoch,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return False
        state_version = (
            int(campaign["state_version"])
            if row["committed_state_version"] is None
            else int(row["committed_state_version"])
        )
        for choice in choices:
            await self._session.execute(
                text(
                    """
                    INSERT INTO turn_choices(
                        id,
                        campaign_id,
                        scene_id,
                        source_turn_id,
                        actor_id,
                        ordinal,
                        label,
                        state_version
                    )
                    VALUES(
                        :id,:c,:s,:t,:a,:o,:label,:v
                    )
                    """
                ),
                {
                    "id": choice.id,
                    "c": campaign_id,
                    "s": row["scene_id"],
                    "t": turn_id,
                    "a": row["actor_id"],
                    "o": choice.ordinal,
                    "label": choice.label,
                    "v": state_version,
                },
            )
        sequence = int(campaign["event_sequence"]) + 1
        payload = NarrationGeneratedPayload(
            narration=narration,
            fallback=fallback_reason is not None,
        )
        await self._session.execute(
            text(
                "UPDATE campaigns SET event_sequence=:sequence WHERE id=:campaign"
            ),
            {"sequence": sequence, "campaign": campaign_id},
        )
        await self._session.execute(
            text(
                """
                INSERT INTO events(
                    id,campaign_id,scene_id,turn_id,action_id,
                    sequence,state_version,type,schema_version,payload
                )
                VALUES(
                    :id,:campaign,:scene,:turn,NULL,
                    :sequence,:version,'GMNarrationGenerated',1,CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": self._event_id_factory(),
                "campaign": campaign_id,
                "scene": row["scene_id"],
                "turn": turn_id,
                "sequence": sequence,
                "version": state_version,
                "payload": _json(payload.model_dump(mode="json")),
            },
        )
        return True


class PostgresUnitOfWork:
    """例外時に必ずrollbackするsession単位Unit of Work。"""

    turns: TurnRepository
    canonical: CanonicalRepository
    narration: NarrationRepository
    llm_calls: LLMCallRepository

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:
        self._session = self._factory()
        self.turns = PostgresTurnRepository(self._session)
        self.canonical = PostgresCanonicalRepository(self._session)
        self.narration = PostgresNarrationRepository(self._session)
        self.llm_calls = PostgresLLMCallRepository(self._session)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        if exc_type is not None:
            await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        assert self._session is not None
        await self._session.commit()

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
