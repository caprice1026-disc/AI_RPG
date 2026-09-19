"""SQLAlchemy async sessionを用いるPostgreSQL Repository実装。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, and_, exists, or_, select, text
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
    FailureDisposition,
    IdempotencyConflictError,
    InvalidCommitBundleError,
    Lease,
    LLMCallRepository,
    LLMPhase,
    NarrationLease,
    NarrationRepository,
    NarrationWorkItem,
    NarrativeCommit,
    PhaseDeadlineExceededError,
    PublicEventRepository,
    RecentMessage,
    ResolutionWorkItem,
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
from ai_rpg.contracts import (
    CampaignStateResponse,
    PlayerTurnInput,
    PublicEvent,
    PublicTurnEventPayload,
    TurnResponse,
)
from ai_rpg.contracts.responses import (
    MechanicalNarrationInput,
    RecoveryReason,
    TurnRecovery,
)
from ai_rpg.domain.commands import AttackCommand, UseItemCommand
from ai_rpg.domain.events import NarrationGeneratedPayload
from ai_rpg.domain.results import AppliedResult, NotApplicableResult
from ai_rpg.infrastructure.postgres.models import (
    ActionModel,
    CampaignMemberModel,
    CampaignModel,
    EntityModel,
    EventModel,
    MvpCharacterModel,
    MvpInventoryModel,
    MvpSceneEntityModel,
    MvpSceneSkillCheckModel,
    MvpSkillModifierModel,
    MvpWeaponModel,
    SceneModel,
    TurnChoiceModel,
    TurnModel,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _public_action_summary(result: Mapping[str, object]) -> str | None:
    """Canonical IDを除いた、公開DTO由来のAction結果だけをContextへ載せる。"""

    kind = result.get("kind")
    if kind == "applied":
        applied = AppliedResult.model_validate(result)
        return _json(
            {
                "kind": applied.kind,
                "outcome": applied.outcome,
                "facts": applied.facts,
                "dice": [die.model_dump(mode="json") for die in applied.dice],
            }
        )[:8000]
    if kind == "not_applicable":
        not_applicable = NotApplicableResult.model_validate(result)
        return _json(
            {"kind": not_applicable.kind, "reason": not_applicable.reason}
        )
    return None


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


_PUBLIC_RECOVERY_REASONS: dict[str, RecoveryReason] = {
    "MODEL_TIMEOUT": "MODEL_TIMEOUT",
    "INVALID_OUTPUT": "INVALID_OUTPUT",
    "MODEL_REFUSAL": "MODEL_REFUSAL",
    "INJECTION_DETECTED": "INJECTION_DETECTED",
    "CONTEXT_CONFLICT": "CONTEXT_CONFLICT",
    "UNKNOWN": "UNKNOWN",
}


def _public_recovery_reason(value: object) -> RecoveryReason | None:
    if value is None:
        return None
    return _PUBLIC_RECOVERY_REASONS.get(str(value), "UNKNOWN")


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


async def _lock_actor_authorization(
    session: AsyncSession,
    campaign_id: UUID,
    principal_id: UUID,
    actor_id: UUID,
) -> bool:
    authorized = (
        await session.execute(
            select(CampaignMemberModel.principal_id)
            .join(
                EntityModel,
                EntityModel.campaign_id == CampaignMemberModel.campaign_id,
            )
            .join(
                CampaignModel,
                CampaignModel.id == CampaignMemberModel.campaign_id,
            )
            .where(
                CampaignModel.id == campaign_id,
                CampaignModel.status == "active",
                CampaignMemberModel.principal_id == principal_id,
                CampaignMemberModel.active.is_(True),
                EntityModel.id == actor_id,
                EntityModel.kind.in_(("pc", "npc")),
                EntityModel.controller_id == principal_id,
                EntityModel.archived_at.is_(None),
            )
            .with_for_update(of=(CampaignMemberModel, EntityModel))
        )
    ).one_or_none()
    return authorized is not None


async def _assert_turn_actor_authorized(
    session: AsyncSession,
    turn: RowMapping,
) -> None:
    if not await _lock_actor_authorization(
        session,
        turn["campaign_id"],
        turn["created_by"],
        turn["actor_id"],
    ):
        raise AuthorizationError("actorを操作する権限が失効しています")


async def _assert_resolution_lease_current(
    session: AsyncSession,
    turn_id: UUID,
    worker_epoch: int,
) -> None:
    validity = (
        await session.execute(
            text(
                """
                SELECT
                    lease_until>clock_timestamp() AS lease_current,
                    resolution_deadline>clock_timestamp() AS deadline_current
                FROM turns
                WHERE id=:turn
                  AND worker_epoch=:epoch
                  AND resolution_status='resolving'
                """
            ),
            {"turn": turn_id, "epoch": worker_epoch},
        )
    ).mappings().one_or_none()
    if validity is None or validity["lease_current"] is not True:
        raise RuntimeError("worker leaseが無効です")
    if validity["deadline_current"] is not True:
        raise PhaseDeadlineExceededError("resolution deadlineを超過しました")


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
        response_row = await self._response_row(existing["campaign_id"], existing["id"])
        assert response_row is not None
        digest = request_hash(
            int(response_row["input_schema_version"]),
            response_row["campaign_id"],
            response_row["scene_id"],
            response_row["created_by"],
            turn,
        )
        if response_row["input_payload"] != turn.model_dump(mode="json") or bytes(
            response_row["request_hash"]
        ) != digest:
            raise IdempotencyConflictError("同じrequest_idに異なる入力は使用できません")
        return self._to_response(response_row)

    @staticmethod
    def _to_response(row: RowMapping) -> TurnResponse:
        return TurnResponse.model_validate(
            {
                "turn_id": row["id"],
                "route": row["route"],
                "resolution_status": row["resolution_status"],
                "narration_status": row["narration_status"],
                "committed_state_version": row["committed_state_version"],
                "narration": row["narration"],
                "choices": row["replay_choices"],
                "action_results": [
                    {
                        "action_id": action["id"],
                        "ordinal": action["ordinal"],
                        "result": action["result"],
                    }
                    for action in row["replay_actions"]
                ],
                "recovery": {
                    "fallback": row["narration_status"] == "fallback",
                    "reason": _public_recovery_reason(row["recovery_reason"]),
                },
            }
        )

    async def _response_row(
        self, campaign_id: UUID, turn_id: UUID
    ) -> RowMapping | None:
        result = await self._session.execute(
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
                WHERE t.id=:turn AND t.campaign_id=:campaign
                """
            ),
            {"turn": turn_id, "campaign": campaign_id},
        )
        return result.mappings().one_or_none()

    async def get_response(
        self, campaign_id: UUID, turn_id: UUID
    ) -> TurnResponse | None:
        row = await self._response_row(campaign_id, turn_id)
        return None if row is None else self._to_response(row)

    async def get_campaign_state(self, campaign_id: UUID) -> CampaignStateResponse:
        state_version = (
            await self._session.execute(
                select(CampaignModel.state_version)
                .where(CampaignModel.id == campaign_id)
                .with_for_update()
            )
        ).scalar_one()
        latest_turn_id = (
            await self._session.execute(
                select(TurnModel.id)
                .where(TurnModel.campaign_id == campaign_id)
                .order_by(TurnModel.created_at.desc(), TurnModel.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latest_turn = (
            None
            if latest_turn_id is None
            else await self.get_response(campaign_id, latest_turn_id)
        )
        return CampaignStateResponse(
            state_version=int(state_version), latest_turn=latest_turn
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
        if not await _lock_actor_authorization(
            self._session,
            campaign_id,
            principal_id,
            turn.actor_id,
        ):
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
        turn_id: UUID | None,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> Lease | None:
        result = await self._session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT id,
                           resolution_started_at IS NOT NULL
                           AND (
                               resolution_attempt_count>=:max_attempts
                               OR resolution_deadline<=clock_timestamp()
                           ) AS terminal_cleanup
                    FROM turns
                    WHERE (CAST(:id AS uuid) IS NULL OR id=CAST(:id AS uuid))
                      AND resolution_status IN ('pending','resolving')
                      AND (resolution_status='pending' OR lease_until<clock_timestamp())
                      AND (
                          (
                              resolution_attempt_count<:max_attempts
                              AND (
                                  resolution_deadline IS NULL
                                  OR resolution_deadline>clock_timestamp()
                              )
                              AND (
                                  resolution_next_attempt_at IS NULL
                                  OR resolution_next_attempt_at<=clock_timestamp()
                              )
                          )
                          OR (
                              resolution_started_at IS NOT NULL
                              AND (
                                  resolution_attempt_count>=:max_attempts
                                  OR resolution_deadline<=clock_timestamp()
                              )
                          )
                      )
                    ORDER BY created_at,id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE turns AS target
                SET resolution_status='resolving',
                    worker_epoch=worker_epoch+1,
                    lease_until=clock_timestamp()+make_interval(secs=>:lease_seconds),
                    resolution_attempt_count=resolution_attempt_count
                        + CASE WHEN candidate.terminal_cleanup THEN 0 ELSE 1 END,
                    resolution_started_at=COALESCE(resolution_started_at,now()),
                    resolution_deadline=COALESCE(
                        resolution_deadline,
                        clock_timestamp()+make_interval(secs=>:deadline_seconds)
                    )
                FROM candidate
                WHERE target.id=candidate.id
                RETURNING target.*,candidate.terminal_cleanup
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
        return (
            None
            if row is None
            else Lease(
                _turn(row),
                row["lease_until"],
                terminal_cleanup=bool(row["terminal_cleanup"]),
            )
        )

    async def _recent_messages(
        self,
        current: RowMapping,
        limit: int,
    ) -> tuple[RecentMessage, ...]:
        if limit == 0:
            return ()
        history = list(
            (
                await self._session.execute(
                    select(
                        TurnModel.id.label("turn_id"),
                        TurnModel.input_text,
                        TurnChoiceModel.label.label("choice_label"),
                        TurnModel.narration,
                        TurnModel.created_at,
                    )
                    .outerjoin(
                        TurnChoiceModel,
                        TurnChoiceModel.id == TurnModel.selected_choice_id,
                    )
                    .where(
                        TurnModel.campaign_id == current["campaign_id"],
                        TurnModel.scene_id == current["scene_id"],
                        TurnModel.actor_id == current["actor_id"],
                        TurnModel.resolution_status.in_(
                            ("committed", "not_applied", "failed")
                        ),
                        TurnModel.narration_status.in_(("completed", "fallback")),
                        or_(
                            TurnModel.created_at < current["created_at"],
                            and_(
                                TurnModel.created_at == current["created_at"],
                                TurnModel.id < current["id"],
                            ),
                        ),
                    )
                    .order_by(TurnModel.created_at.desc(), TurnModel.id.desc())
                    .limit(limit)
                )
            ).mappings()
        )
        if not history:
            return ()

        turn_ids = [row["turn_id"] for row in history]
        action_rows = (
            await self._session.execute(
                select(ActionModel.turn_id, ActionModel.ordinal, ActionModel.result)
                .where(ActionModel.turn_id.in_(turn_ids))
                .order_by(ActionModel.turn_id, ActionModel.ordinal)
            )
        ).mappings()
        actions_by_turn: dict[UUID, list[str]] = {}
        for action in action_rows:
            summary = _public_action_summary(action["result"])
            if summary is not None:
                actions_by_turn.setdefault(action["turn_id"], []).append(summary)

        messages: list[RecentMessage] = []
        for row in reversed(history):
            player_text = row["input_text"] or row["choice_label"]
            if player_text:
                messages.append(RecentMessage("recent_player", str(player_text)[:8000]))
            messages.extend(
                RecentMessage("recent_action_result", summary)
                for summary in actions_by_turn.get(row["turn_id"], ())
            )
            if row["narration"]:
                messages.append(RecentMessage("recent_gm", str(row["narration"])[:8000]))
        return tuple(messages[-limit:])

    async def get_resolution_work(
        self,
        turn_id: UUID,
        worker_epoch: int,
        *,
        recent_messages_limit: int,
    ) -> ResolutionWorkItem | None:
        if not 0 <= recent_messages_limit <= 100:
            raise ValueError("recent_messages_limitは0から100の範囲で指定してください")
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT
                        t.id,
                        t.campaign_id,
                        t.scene_id,
                        t.created_by,
                        t.actor_id,
                        EXISTS(
                            SELECT 1
                            FROM campaigns AS campaign
                            JOIN campaign_members AS member
                              ON member.campaign_id=t.campaign_id
                             AND member.principal_id=t.created_by
                            JOIN entities AS actor
                              ON actor.campaign_id=t.campaign_id
                             AND actor.id=t.actor_id
                            WHERE campaign.id=t.campaign_id
                              AND campaign.status='active'
                              AND member.active
                              AND actor.kind IN ('pc','npc')
                              AND actor.controller_id=t.created_by
                              AND actor.archived_at IS NULL
                        ) AS actor_authorized,
                        t.worker_epoch,
                        t.max_actions,
                        t.expected_state_version,
                        t.route,
                        t.created_at,
                        COALESCE(t.input_text,c.label) AS player_text
                    FROM turns AS t
                    LEFT JOIN turn_choices AS c ON c.id=t.selected_choice_id
                    WHERE t.id=:turn
                      AND t.worker_epoch=:epoch
                      AND t.resolution_status='resolving'
                      AND t.lease_until>clock_timestamp()
                    """
                ),
                {"turn": turn_id, "epoch": worker_epoch},
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        recent_messages = await self._recent_messages(row, recent_messages_limit)
        return ResolutionWorkItem(
            turn_id=row["id"],
            campaign_id=row["campaign_id"],
            scene_id=row["scene_id"],
            principal_id=row["created_by"],
            actor_id=row["actor_id"],
            actor_authorized=bool(row["actor_authorized"]),
            worker_epoch=int(row["worker_epoch"]),
            max_actions=int(row["max_actions"]),
            expected_state_version=int(row["expected_state_version"]),
            player_text=str(row["player_text"]),
            recent_messages=recent_messages,
            route=row["route"],
        )

    async def record_initial_route(
        self,
        turn_id: UUID,
        worker_epoch: int,
        route: Literal["narrative", "mechanical"],
        rule_version: str,
        reason_codes: Sequence[str],
    ) -> bool:
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET route=COALESCE(route,:route),
                    initial_route=COALESCE(initial_route,:route),
                    routing_rule_version=COALESCE(routing_rule_version,:version),
                    routing_reason_codes=COALESCE(
                        routing_reason_codes,
                        CAST(:reasons AS jsonb)
                    )
                WHERE id=:turn
                  AND worker_epoch=:epoch
                  AND resolution_status='resolving'
                  AND lease_until>clock_timestamp()
                  AND (
                      initial_route IS NULL
                      OR (
                          initial_route=:route
                          AND routing_rule_version=:version
                          AND routing_reason_codes=CAST(:reasons AS jsonb)
                      )
                  )
                RETURNING id
                """
            ),
            {
                "turn": turn_id,
                "epoch": worker_epoch,
                "route": route,
                "version": rule_version,
                "reasons": _json(list(reason_codes)),
            },
        )
        return result.scalar_one_or_none() is not None

    async def promote_to_mechanical(self, turn_id: UUID, worker_epoch: int) -> bool:
        result = await self._session.execute(
            sql_update(TurnModel)
            .where(
                TurnModel.id == turn_id,
                TurnModel.worker_epoch == worker_epoch,
                TurnModel.resolution_status == "resolving",
                TurnModel.lease_until > text("clock_timestamp()"),
                TurnModel.route == "narrative",
            )
            .values(route="mechanical")
            .returning(TurnModel.id)
        )
        return result.scalar_one_or_none() is not None

    async def finalize_not_applied(
        self, turn_id: UUID, worker_epoch: int, narration: str
    ) -> bool:
        campaign_id = await self._session.scalar(
            select(TurnModel.campaign_id).where(TurnModel.id == turn_id)
        )
        if campaign_id is None:
            return False
        await self._session.execute(
            select(CampaignModel.id)
            .where(CampaignModel.id == campaign_id)
            .with_for_update()
        )
        turn = (
            await self._session.execute(
                select(TurnModel).where(TurnModel.id == turn_id).with_for_update()
            )
        ).scalar_one()
        if (
            turn.resolution_status == "not_applied"
            and turn.narration_status == "completed"
        ):
            return True
        if turn.resolution_status != "resolving" or turn.worker_epoch != worker_epoch:
            return False
        await _assert_resolution_lease_current(self._session, turn_id, worker_epoch)
        campaign = (
            await self._session.execute(
                select(CampaignModel.state_version, CampaignModel.event_sequence).where(
                    CampaignModel.id == campaign_id
                )
            )
        ).one()
        sequence = int(campaign.event_sequence) + 1
        await self._session.execute(
            sql_update(CampaignModel)
            .where(CampaignModel.id == campaign_id)
            .values(event_sequence=sequence)
        )
        await self._session.execute(
            sql_update(TurnModel)
            .where(TurnModel.id == turn_id)
            .values(
                resolution_status="not_applied",
                narration_status="completed",
                narration=narration,
                lease_until=None,
            )
        )
        payload = NarrationGeneratedPayload(narration=narration, fallback=False)
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
                "scene": turn.scene_id,
                "turn": turn_id,
                "sequence": sequence,
                "version": int(campaign.state_version),
                "payload": _json(payload.model_dump(mode="json")),
            },
        )
        return True

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
                    text(
                        "SELECT * FROM turns "
                        "WHERE id=:t AND campaign_id=:c FOR UPDATE"
                    ),
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
        ):
            raise RuntimeError("worker leaseが無効です")
        await _assert_turn_actor_authorized(self._session, turn)
        if int(campaign["state_version"]) != bundle.base_state_version:
            raise StateVersionConflictError("Canonical versionが更新されています")
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
        await _assert_resolution_lease_current(
            self._session, bundle.turn_id, bundle.worker_epoch
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

    async def commit_narrative(self, commit: NarrativeCommit) -> int:
        campaign = (
            await self._session.execute(
                text(
                    "SELECT state_version,event_sequence FROM campaigns "
                    "WHERE id=:campaign FOR UPDATE"
                ),
                {"campaign": commit.campaign_id},
            )
        ).mappings().one()
        turn = (
            await self._session.execute(
                text(
                    "SELECT * FROM turns "
                    "WHERE id=:turn AND campaign_id=:campaign FOR UPDATE"
                ),
                {"turn": commit.turn_id, "campaign": commit.campaign_id},
            )
        ).mappings().one()
        if (
            turn["resolution_status"] == "committed"
            and turn["narration_status"] == "completed"
        ):
            return int(turn["committed_state_version"])
        if (
            turn["resolution_status"] != "resolving"
            or turn["route"] != "narrative"
            or int(turn["worker_epoch"]) != commit.worker_epoch
        ):
            raise RuntimeError("worker leaseが無効です")
        await _assert_turn_actor_authorized(self._session, turn)
        if int(campaign["state_version"]) != commit.base_state_version:
            raise StateVersionConflictError("Canonical versionが更新されています")
        await _assert_resolution_lease_current(
            self._session, commit.turn_id, commit.worker_epoch
        )

        version = int(campaign["state_version"])
        sequence = int(campaign["event_sequence"]) + 1
        await self._session.execute(
            text(
                "UPDATE campaigns SET event_sequence=:sequence WHERE id=:campaign"
            ),
            {"sequence": sequence, "campaign": commit.campaign_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE turns
                SET resolution_status='committed',
                    narration_status='completed',
                    committed_state_version=:version,
                    committed_at=now(),
                    narration=:narration,
                    lease_until=NULL
                WHERE id=:turn
                """
            ),
            {
                "version": version,
                "narration": commit.narration,
                "turn": commit.turn_id,
            },
        )
        for choice in commit.choices:
            await self._session.execute(
                text(
                    """
                    INSERT INTO turn_choices(
                        id,campaign_id,scene_id,source_turn_id,actor_id,
                        ordinal,label,state_version
                    )
                    VALUES(:id,:campaign,:scene,:turn,:actor,:ordinal,:label,:version)
                    """
                ),
                {
                    "id": choice.id,
                    "campaign": commit.campaign_id,
                    "scene": commit.scene_id,
                    "turn": commit.turn_id,
                    "actor": turn["actor_id"],
                    "ordinal": choice.ordinal,
                    "label": choice.label,
                    "version": version,
                },
            )
        payload = NarrationGeneratedPayload(narration=commit.narration, fallback=False)
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
                "campaign": commit.campaign_id,
                "scene": commit.scene_id,
                "turn": commit.turn_id,
                "sequence": sequence,
                "version": version,
                "payload": _json(payload.model_dump(mode="json")),
            },
        )
        return version


class PostgresCanonicalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, campaign_id: UUID, scene_id: UUID) -> CanonicalSnapshot:
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

        scene_entities = await self._session.execute(
            select(MvpSceneEntityModel.__table__).where(
                MvpSceneEntityModel.campaign_id == campaign_id,
                MvpSceneEntityModel.scene_id == scene_id,
            )
        )

        return CanonicalSnapshot(
            campaign_id,
            int(campaign[0]),
            await rows(MvpCharacterModel.__table__),
            await rows(MvpSkillModifierModel.__table__),
            await rows(MvpWeaponModel.__table__),
            await rows(MvpInventoryModel.__table__),
            await rows(MvpSceneSkillCheckModel.__table__),
            await rows(EntityModel.__table__),
            scene_entities=tuple(dict(row) for row in scene_entities.mappings()),
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
                "AND worker_epoch=:epoch AND lease_until>clock_timestamp() "
                "AND resolution_deadline>clock_timestamp()"
            ),
            "narration": (
                "narration_status='generating' "
                "AND narration_worker_epoch=:epoch "
                "AND narration_lease_until>clock_timestamp() "
                "AND narration_deadline>clock_timestamp()"
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

    async def record_failure(
        self,
        turn_id: UUID,
        *,
        phase: LLMPhase,
        worker_epoch: int,
        failure_code: str,
        max_attempts: int,
    ) -> FailureDisposition | None:
        campaign_id = await self._session.scalar(
            select(TurnModel.campaign_id).where(TurnModel.id == turn_id)
        )
        if campaign_id is None:
            return None
        await self._session.execute(
            select(CampaignModel.id)
            .where(CampaignModel.id == campaign_id)
            .with_for_update()
        )
        if phase == "resolution":
            ownership = (
                "resolution_status='resolving' "
                "AND worker_epoch=:epoch AND lease_until>clock_timestamp()"
            )
            attempts = "resolution_attempt_count"
            deadline = "resolution_deadline"
        elif phase == "narration":
            ownership = (
                "narration_status='generating' "
                "AND narration_worker_epoch=:epoch "
                "AND narration_lease_until>clock_timestamp()"
            )
            attempts = "narration_attempt_count"
            deadline = "narration_deadline"
        else:
            raise ValueError(f"未対応のLLM phaseです: {phase}")

        row = (
            await self._session.execute(
                text(
                    f"""
                    SELECT
                        ({attempts}<:max_attempts
                         AND {deadline}>clock_timestamp()
                         AND llm_call_count<llm_call_budget
                         AND (route IS DISTINCT FROM 'narrative' OR llm_call_count<1)
                        ) AS retry
                    FROM turns
                    WHERE id=:turn AND {ownership}
                    FOR UPDATE
                    """
                ),
                {
                    "turn": turn_id,
                    "epoch": worker_epoch,
                    "max_attempts": max_attempts,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        retry = bool(row["retry"])
        if phase == "resolution":
            await self._session.execute(
                text(
                    """
                    UPDATE turns
                    SET resolution_status=:status,
                        lease_until=NULL,
                        resolution_next_attempt_at=CASE
                            WHEN :retry THEN clock_timestamp() ELSE NULL
                        END,
                        resolution_failure_code=:code
                    WHERE id=:turn AND worker_epoch=:epoch
                    """
                ),
                {
                    "status": "pending" if retry else "failed",
                    "retry": retry,
                    "code": failure_code,
                    "turn": turn_id,
                    "epoch": worker_epoch,
                },
            )
        else:
            await self._session.execute(
                text(
                    """
                    UPDATE turns
                    SET narration_status=CASE WHEN :retry THEN 'pending' ELSE 'generating' END,
                        narration_lease_until=CASE
                            WHEN :retry THEN NULL ELSE narration_lease_until
                        END,
                        narration_next_attempt_at=CASE
                            WHEN :retry THEN clock_timestamp() ELSE NULL
                        END,
                        narration_failure_code=:code
                    WHERE id=:turn AND narration_worker_epoch=:epoch
                    """
                ),
                {
                    "retry": retry,
                    "code": failure_code,
                    "turn": turn_id,
                    "epoch": worker_epoch,
                },
            )
        return "retry" if retry else "terminal"


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
        turn_id: UUID | None,
        *,
        lease_seconds: int,
        max_attempts: int,
        deadline_seconds: int,
    ) -> NarrationLease | None:
        result = await self._session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT id,
                           narration_started_at IS NOT NULL
                           AND (
                               narration_attempt_count>=:max_attempts
                               OR narration_deadline<=clock_timestamp()
                           ) AS terminal_cleanup
                    FROM turns
                    WHERE (CAST(:id AS uuid) IS NULL OR id=CAST(:id AS uuid))
                      AND resolution_status IN ('committed','not_applied','failed')
                      AND narration_status IN ('pending','generating')
                      AND (
                          narration_status='pending'
                          OR narration_lease_until<clock_timestamp()
                      )
                      AND (
                          (
                              narration_attempt_count<:max_attempts
                              AND (
                                  narration_deadline IS NULL
                                  OR narration_deadline>clock_timestamp()
                              )
                              AND (
                                  narration_next_attempt_at IS NULL
                                  OR narration_next_attempt_at<=clock_timestamp()
                              )
                          )
                          OR (
                              narration_started_at IS NOT NULL
                              AND (
                                  narration_attempt_count>=:max_attempts
                                  OR narration_deadline<=clock_timestamp()
                              )
                          )
                      )
                    ORDER BY created_at,id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE turns AS target
                SET narration_status='generating',
                    narration_worker_epoch=narration_worker_epoch+1,
                    narration_lease_until=clock_timestamp()+make_interval(secs=>:lease_seconds),
                    narration_attempt_count=narration_attempt_count
                        + CASE WHEN candidate.terminal_cleanup THEN 0 ELSE 1 END,
                    narration_started_at=COALESCE(narration_started_at,now()),
                    narration_deadline=COALESCE(
                        narration_deadline,
                        clock_timestamp()+make_interval(secs=>:deadline_seconds)
                    )
                FROM candidate
                WHERE target.id=candidate.id
                RETURNING
                    target.id,
                    target.campaign_id,
                    target.narration_worker_epoch,
                    target.narration_lease_until,
                    candidate.terminal_cleanup
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
            terminal_cleanup=bool(row["terminal_cleanup"]),
        )

    async def get_work(
        self, turn_id: UUID, worker_epoch: int
    ) -> NarrationWorkItem | None:
        row = (
            await self._session.execute(
                select(
                    TurnModel.id,
                    TurnModel.campaign_id,
                    TurnModel.narration_worker_epoch,
                    TurnModel.narration_input,
                ).where(
                    TurnModel.id == turn_id,
                    TurnModel.narration_worker_epoch == worker_epoch,
                    TurnModel.narration_status == "generating",
                    TurnModel.narration_lease_until > text("clock_timestamp()"),
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return NarrationWorkItem(
            turn_id=row["id"],
            campaign_id=row["campaign_id"],
            worker_epoch=int(row["narration_worker_epoch"]),
            narration_input=(
                None
                if row["narration_input"] is None
                else MechanicalNarrationInput.model_validate(row["narration_input"])
            ),
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
                  AND narration_lease_until>clock_timestamp()
                  AND (:fallback OR narration_deadline>clock_timestamp())
                RETURNING scene_id,actor_id,committed_state_version
                """
            ),
            {
                "status": "fallback" if fallback_reason else "completed",
                "n": narration,
                "reason": fallback_reason,
                "fallback": fallback_reason is not None,
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


class PostgresPublicEventRepository:
    """永続event sequenceを秘密を含まない公開Turn表現へ投影する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_after(
        self, campaign_id: UUID, after: int, *, limit: int
    ) -> tuple[PublicEvent, ...]:
        rows = (
            await self._session.execute(
                select(EventModel.sequence, EventModel.turn_id)
                .where(
                    EventModel.campaign_id == campaign_id,
                    EventModel.sequence > after,
                    EventModel.turn_id.is_not(None),
                )
                .order_by(EventModel.sequence)
                .limit(limit)
            )
        ).all()
        turns = PostgresTurnRepository(self._session)
        public: list[PublicEvent] = []
        for sequence, turn_id in rows:
            if turn_id is None:
                continue
            response = await turns.get_response(campaign_id, turn_id)
            if response is None:
                continue
            public.append(
                PublicEvent(
                    id=int(sequence),
                    type="turn.updated",
                    schema_version=1,
                    payload=PublicTurnEventPayload(turn=response),
                )
            )
        return tuple(public)


class PostgresUnitOfWork:
    """例外時に必ずrollbackするsession単位Unit of Work。"""

    turns: TurnRepository
    canonical: CanonicalRepository
    narration: NarrationRepository
    llm_calls: LLMCallRepository
    events: PublicEventRepository

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:
        self._session = self._factory()
        self.turns = PostgresTurnRepository(self._session)
        self.canonical = PostgresCanonicalRepository(self._session)
        self.narration = PostgresNarrationRepository(self._session)
        self.llm_calls = PostgresLLMCallRepository(self._session)
        self.events = PostgresPublicEventRepository(self._session)
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
