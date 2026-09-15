"""SQLAlchemy async sessionを用いるPostgreSQL Repository実装。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_rpg.application.ports.repositories import (
    CanonicalRepository,
    CanonicalSnapshot,
    ChoiceDraft,
    CommitBundle,
    IdempotencyConflictError,
    Lease,
    NarrationRepository,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
)
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.contracts.responses import TurnRecovery


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


class PostgresTurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        campaign_id: UUID,
        principal_id: UUID,
        turn: PlayerTurnInput,
        max_actions: int,
    ) -> TurnResponse:
        """既存Application API向けにactive Sceneへpending Turnを追加する。"""
        existing = await self._request_row(campaign_id, principal_id, turn.request_id)
        if existing is not None:
            return await self._replay(existing, turn)

        row = await self.accept_pending(
            campaign_id,
            principal_id,
            turn,
            max_actions=max_actions,
        )
        if row is None:
            existing = await self._request_row(campaign_id, principal_id, turn.request_id)
            if existing is None:
                raise TurnInProgressError("別の未解決Turnが存在します")
            return await self._replay(existing, turn)
        return _pending_response(row)

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
            text("SELECT * FROM turns WHERE campaign_id=:c AND created_by=:p AND request_id=:r"),
            {"c": campaign_id, "p": principal_id, "r": request_id},
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
    ) -> TurnRow | None:
        # 全Canonical更新経路と同じくCampaignを最初にロックする。
        await self._session.execute(
            text("SELECT id FROM campaigns WHERE id=:c FOR UPDATE"), {"c": campaign_id}
        )
        unresolved_turn = await self._session.execute(
            text(
                "SELECT id FROM turns "
                "WHERE campaign_id=:c AND resolution_status IN ('pending','resolving')"
            ),
            {"c": campaign_id},
        )
        if unresolved_turn.scalar_one_or_none() is not None:
            return None
        scene_id = (
            await self._session.execute(
                text("SELECT id FROM scenes WHERE campaign_id=:c AND status='active'"),
                {"c": campaign_id},
            )
        ).scalar_one()
        content = turn.content.model_dump(mode="json")
        params = {
            "id": uuid4(),
            "c": campaign_id,
            "s": scene_id,
            "p": principal_id,
            "r": turn.request_id,
            "a": turn.actor_id,
            "payload": _json(turn.model_dump(mode="json")),
            "hash": request_hash(1, campaign_id, scene_id, principal_id, turn),
            "kind": content["kind"],
            "input_text": content.get("text"),
            "choice": content.get("choice_id"),
            "version": turn.expected_state_version,
            "max_actions": max_actions,
        }
        result = await self._session.execute(
            text(
                """
                INSERT INTO turns(
                    id,
                    campaign_id,
                    scene_id,
                    request_id,
                    created_by,
                    actor_id,
                    input_payload,
                    request_hash,
                    input_kind,
                    input_text,
                    selected_choice_id,
                    expected_state_version,
                    max_actions
                )
                VALUES(
                    :id,
                    :c,
                    :s,
                    :r,
                    :p,
                    :a,
                    CAST(:payload AS jsonb),
                    :hash,
                    :kind,
                    :input_text,
                    :choice,
                    :version,
                    :max_actions
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """
            ),
            params,
        )
        row = result.mappings().one_or_none()
        return None if row is None else _turn(row)

    async def acquire_lease(self, turn_id: UUID, *, lease_seconds: int) -> Lease | None:
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET resolution_status='resolving',
                    worker_epoch=worker_epoch+1,
                    lease_until=now()+make_interval(secs=>:seconds)
                WHERE id=(
                    SELECT id
                    FROM turns
                    WHERE id=:id
                      AND resolution_status IN ('pending','resolving')
                      AND (resolution_status='pending' OR lease_until<now())
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """
            ),
            {"id": turn_id, "seconds": lease_seconds},
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
        version = bundle.base_state_version + int(bundle.canonical_changed)
        for entity_id, hp, max_hp in bundle.canonical_updates:
            await self._session.execute(
                text(
                    """
                    UPDATE mvp_characters
                    SET current_hp=:hp,max_hp=:max_hp
                    WHERE campaign_id=:c AND entity_id=:e
                    """
                ),
                {"hp": hp, "max_hp": max_hp, "c": bundle.campaign_id, "e": entity_id},
            )
        await self._session.execute(
            text(
                "UPDATE campaigns SET state_version=:v,event_sequence=event_sequence+:n WHERE id=:c"
            ),
            {"v": version, "n": len(bundle.events), "c": bundle.campaign_id},
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
            {"v": version, "ni": _json(bundle.narration_input), "t": bundle.turn_id},
        )
        for action in bundle.actions:
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
                    "id": action.id,
                    "c": bundle.campaign_id,
                    "t": bundle.turn_id,
                    "o": action.ordinal,
                    "actor": action.actor_id,
                    "kind": action.kind,
                    "target": action.target_id,
                    "item": action.item_id,
                    "command": _json(action.command),
                    "result": _json(action.result),
                    "rk": action.result_kind,
                    "ruleset": campaign["ruleset_version"],
                },
            )
        first = int(campaign["event_sequence"]) + 1
        for offset, event in enumerate(bundle.events):
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
                    "seq": first + offset,
                    "v": version,
                    "type": event.event_type,
                    "payload": _json(event.payload),
                },
            )
        return version


class PostgresCanonicalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def snapshot(self, campaign_id: UUID) -> CanonicalSnapshot:
        campaign = (
            await self._session.execute(
                text("SELECT state_version FROM campaigns WHERE id=:c"), {"c": campaign_id}
            )
        ).one()

        async def rows(table: str) -> tuple[Mapping[str, object], ...]:
            result = await self._session.execute(
                text(f"SELECT * FROM {table} WHERE campaign_id=:c"), {"c": campaign_id}
            )
            return tuple(dict(row) for row in result.mappings())

        return CanonicalSnapshot(
            campaign_id,
            int(campaign[0]),
            await rows("mvp_characters"),
            await rows("mvp_skill_modifiers"),
            await rows("mvp_weapons"),
            await rows("mvp_inventory"),
        )

    async def update_with_campaign_lock(
        self, campaign_id: UUID, update: Callable[[], object]
    ) -> int:
        row = (
            await self._session.execute(
                text("SELECT state_version FROM campaigns WHERE id=:c FOR UPDATE"),
                {"c": campaign_id},
            )
        ).one()
        value = update()
        if hasattr(value, "__await__"):
            await value
        version = int(row[0]) + 1
        await self._session.execute(
            text("UPDATE campaigns SET state_version=:v WHERE id=:c"),
            {"v": version, "c": campaign_id},
        )
        return version


class PostgresNarrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        await self._session.execute(
            text("SELECT id FROM campaigns WHERE id=:c FOR UPDATE"), {"c": campaign_id}
        )
        result = await self._session.execute(
            text(
                """
                UPDATE turns
                SET narration_status=:status,
                    narration=:n,
                    recovery_reason=:reason
                WHERE id=:t
                  AND campaign_id=:c
                  AND worker_epoch=:epoch
                  AND narration_status IN ('pending','generating')
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
                    "v": row["committed_state_version"],
                },
            )
        return True


class PostgresUnitOfWork:
    """例外時に必ずrollbackするsession単位Unit of Work。"""

    turns: TurnRepository
    canonical: CanonicalRepository
    narration: NarrationRepository

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:
        self._session = self._factory()
        self.turns = PostgresTurnRepository(self._session)
        self.canonical = PostgresCanonicalRepository(self._session)
        self.narration = PostgresNarrationRepository(self._session)
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
