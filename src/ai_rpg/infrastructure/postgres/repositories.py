"""SQLAlchemy async sessionを用いるPostgreSQL Repository実装。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    NarrationRepository,
    StateVersionConflictError,
    TurnInProgressError,
    TurnRepository,
    TurnRow,
)
from ai_rpg.contracts import PlayerTurnInput, TurnResponse
from ai_rpg.contracts.responses import MechanicalNarrationInput, TurnRecovery
from ai_rpg.domain.commands import AttackCommand as ContractAttackCommand
from ai_rpg.domain.commands import Command
from ai_rpg.domain.commands import UseItemCommand as ContractUseItemCommand
from ai_rpg.domain.events import (
    ActionResolvedEvent,
    DamageAppliedEvent,
    DiceRolledEvent,
    DomainEventV1,
)
from ai_rpg.domain.results import ActionResult, AppliedResult, DamageFact, ResolvedAction

_COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)
_RESULT_ADAPTER: TypeAdapter[ActionResult] = TypeAdapter(ActionResult)
_EVENT_ADAPTER: TypeAdapter[DomainEventV1] = TypeAdapter(DomainEventV1)


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


def _resolution_projection(
    bundle: CommitBundle,
    turn: RowMapping,
    *,
    version: int,
    first_event_sequence: int,
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    dict[str, object],
    tuple[DamageFact, ...],
]:
    """Bundleを既存契約へ投影し、SQLへ渡す整合済み値だけを返す。"""

    if bundle.scene_id != turn["scene_id"]:
        raise InvalidCommitBundleError("BundleのSceneがTurnと一致しません")
    max_actions = int(turn["max_actions"])
    if not 1 <= len(bundle.actions) <= max_actions:
        raise InvalidCommitBundleError("Action件数がTurnの上限外です")
    if [action.ordinal for action in bundle.actions] != list(range(1, len(bundle.actions) + 1)):
        raise InvalidCommitBundleError("Action ordinalは1から連続する必要があります")
    if len({action.id for action in bundle.actions}) != len(bundle.actions):
        raise InvalidCommitBundleError("Action IDが重複しています")

    action_params: list[dict[str, object]] = []
    resolved_actions: list[ResolvedAction] = []
    results: list[ActionResult] = []
    try:
        for action in bundle.actions:
            command = _COMMAND_ADAPTER.validate_python(action.command)
            result = _RESULT_ADAPTER.validate_python(action.result)
            target_id = command.target_id
            if isinstance(command, ContractAttackCommand):
                item_id = command.weapon_id
            elif isinstance(command, ContractUseItemCommand):
                item_id = command.item_id
            else:
                item_id = None
            if (
                command.action_id != action.id
                or command.campaign_id != bundle.campaign_id
                or command.turn_id != bundle.turn_id
                or command.actor_id != action.actor_id
                or command.actor_id != turn["actor_id"]
                or command.ordinal != action.ordinal
                or command.kind != action.kind
                or target_id != action.target_id
                or item_id != action.item_id
                or result.kind != action.result_kind
            ):
                raise InvalidCommitBundleError("ActionRecordとCommand/Resultが一致しません")
            action_params.append(
                {
                    "id": action.id,
                    "o": action.ordinal,
                    "actor": action.actor_id,
                    "kind": action.kind,
                    "target": target_id,
                    "item": item_id,
                    "command": command.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                    "rk": result.kind,
                }
            )
            resolved_actions.append(
                ResolvedAction(action_id=action.id, ordinal=action.ordinal, result=result)
            )
            results.append(result)

        narration_input = MechanicalNarrationInput.model_validate(bundle.narration_input)
        if (
            narration_input.committed_state_version != version
            or narration_input.output_limits.max_actions != max_actions
            or narration_input.resolved_actions != resolved_actions
        ):
            raise InvalidCommitBundleError("描写入力が確定内容と一致しません")

        if len({event.id for event in bundle.events}) != len(bundle.events):
            raise InvalidCommitBundleError("Event IDが重複しています")
        events = tuple(
            _EVENT_ADAPTER.validate_python(
                {
                    "id": event.id,
                    "campaign_id": bundle.campaign_id,
                    "scene_id": bundle.scene_id,
                    "turn_id": bundle.turn_id,
                    "action_id": event.action_id,
                    "sequence": first_event_sequence + offset,
                    "state_version": version,
                    "schema_version": 1,
                    "type": event.event_type,
                    "payload": event.payload,
                }
            )
            for offset, event in enumerate(bundle.events)
        )
    except ValidationError as error:
        raise InvalidCommitBundleError("Bundleが既存契約を満たしません") from error

    expected_events: list[tuple[str, UUID, object]] = []
    for action, result in zip(bundle.actions, results, strict=True):
        if isinstance(result, AppliedResult):
            expected_events.extend(("DiceRolled", action.id, roll) for roll in result.dice)
            expected_events.extend(("DamageApplied", action.id, damage) for damage in result.damage)
        expected_events.append(("ActionResolved", action.id, result))
    if [(event.type, event.action_id) for event in events] != [
        (event_type, action_id) for event_type, action_id, _ in expected_events
    ]:
        raise InvalidCommitBundleError("Event件数または順序がAction結果と一致しません")

    for event, (_, _, expected_payload) in zip(events, expected_events, strict=True):
        if isinstance(event, DiceRolledEvent):
            valid = event.payload.roll == expected_payload
        elif isinstance(event, DamageAppliedEvent):
            valid = event.payload == expected_payload
        elif isinstance(event, ActionResolvedEvent):
            valid = event.payload.result == expected_payload
        else:
            valid = False
        if not valid:
            raise InvalidCommitBundleError("Event payloadがAction結果と一致しません")

    event_params: list[dict[str, object]] = [
        {
            "id": event.id,
            "a": event.action_id,
            "seq": event.sequence,
            "type": event.type,
            "payload": event.payload.model_dump(mode="json"),
        }
        for event in events
    ]
    damage = tuple(
        fact for result in results if isinstance(result, AppliedResult) for fact in result.damage
    )
    return (
        tuple(action_params),
        tuple(event_params),
        narration_input.model_dump(mode="json"),
        damage,
    )


async def _canonical_update_projection(
    session: AsyncSession,
    bundle: CommitBundle,
) -> tuple[dict[str, object], ...]:
    """Canonical更新を検証し、既存行をlockしたSQL parameterへ変換する。"""

    updates = tuple(bundle.canonical_updates)
    if type(bundle.canonical_changed) is not bool or bundle.canonical_changed != bool(updates):
        raise InvalidCommitBundleError("Canonical変更フラグと更新内容が一致しません")

    params: list[dict[str, object]] = []
    entity_ids: set[UUID] = set()
    for update in updates:
        try:
            entity_id, hp, max_hp = update
        except (TypeError, ValueError) as error:
            raise InvalidCommitBundleError("Canonical更新の形式が不正です") from error
        if (
            not isinstance(entity_id, UUID)
            or type(hp) is not int
            or type(max_hp) is not int
            or max_hp < 1
            or not 0 <= hp <= max_hp
        ):
            raise InvalidCommitBundleError("Canonical更新値が不正です")
        if entity_id in entity_ids:
            raise InvalidCommitBundleError("Canonical更新対象が重複しています")
        entity_ids.add(entity_id)

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
                {"c": bundle.campaign_id, "e": entity_id},
            )
        ).one_or_none()
        if current is None:
            raise InvalidCommitBundleError("Canonical更新対象が存在しません")
        if (hp, max_hp) == (int(current.current_hp), int(current.max_hp)):
            raise InvalidCommitBundleError("Canonical更新に実際の変更がありません")
        params.append(
            {
                "hp": hp,
                "max_hp": max_hp,
                "c": bundle.campaign_id,
                "e": entity_id,
                "current_hp": int(current.current_hp),
                "current_max_hp": int(current.max_hp),
            }
        )
    return tuple(params)


def _validate_canonical_damage(
    canonical_params: tuple[dict[str, object], ...],
    damage: tuple[DamageFact, ...],
) -> None:
    """Damage結果と一度だけ適用するHP更新の対応を確認する。"""

    chains: dict[UUID, tuple[int, int]] = {}
    for fact in damage:
        if fact.hp_after != max(0, fact.hp_before - fact.amount):
            raise InvalidCommitBundleError("Damage結果のHP計算が不正です")
        chain = chains.get(fact.target_id)
        if chain is not None and fact.hp_before != chain[1]:
            raise InvalidCommitBundleError("Damage結果のHP遷移が連続していません")
        chains[fact.target_id] = (
            fact.hp_before if chain is None else chain[0],
            fact.hp_after,
        )

    updates = {params["e"]: params for params in canonical_params}
    changed_targets = {
        target_id for target_id, (hp_before, hp_after) in chains.items() if hp_before != hp_after
    }
    if set(updates) != changed_targets:
        raise InvalidCommitBundleError("Canonical更新がDamage結果と一致しません")
    for target_id in changed_targets:
        params = updates[target_id]
        hp_before, hp_after = chains[target_id]
        if (
            params["current_hp"] != hp_before
            or params["hp"] != hp_after
            or params["max_hp"] != params["current_max_hp"]
        ):
            raise InvalidCommitBundleError("Canonical HP更新がDamage結果と一致しません")


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
        )
        if row is None:
            existing = await self._request_row(campaign_id, principal_id, turn.request_id)
            if existing is None:
                raise TurnInProgressError("別の未解決Turnが存在します")
            return await self._replay(existing, turn)
        return _pending_response(row)

    async def _can_access_campaign(self, campaign_id: UUID, principal_id: UUID) -> bool:
        result = await self._session.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM campaign_members "
                "WHERE campaign_id=:c AND principal_id=:p AND active)"
            ),
            {"c": campaign_id, "p": principal_id},
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
        campaign = (
            await self._session.execute(
                text("SELECT state_version,status FROM campaigns WHERE id=:c FOR UPDATE"),
                {"c": campaign_id},
            )
        ).mappings().one()
        if not await self._can_access_campaign(campaign_id, principal_id):
            raise AuthorizationError("Campaignを参照する権限がありません")
        if await self._request_row(campaign_id, principal_id, turn.request_id) is not None:
            return None
        if campaign["status"] != "active":
            raise AuthorizationError("停止中のCampaignへTurnは追加できません")
        actor_allowed = await self._session.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM entities AS e
                    JOIN campaign_members AS m
                      ON m.campaign_id=e.campaign_id AND m.principal_id=:p
                    WHERE e.campaign_id=:c
                      AND e.id=:a
                      AND e.kind IN ('pc','npc')
                      AND e.controller_id=:p
                      AND e.archived_at IS NULL
                      AND m.active
                )
                """
            ),
            {"c": campaign_id, "p": principal_id, "a": turn.actor_id},
        )
        if not actor_allowed.scalar_one():
            raise AuthorizationError("actorを操作する権限がありません")
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
            text(
                "UPDATE turn_choices SET invalidated_at=now() "
                "WHERE campaign_id=:c AND actor_id=:a AND invalidated_at IS NULL"
            ),
            {"c": campaign_id, "a": turn.actor_id},
        )
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
        canonical_params = await _canonical_update_projection(self._session, bundle)
        version = bundle.base_state_version + int(bool(canonical_params))
        first = int(campaign["event_sequence"]) + 1
        action_params, event_params, narration_input, damage = _resolution_projection(
            bundle,
            turn,
            version=version,
            first_event_sequence=first,
        )
        _validate_canonical_damage(canonical_params, damage)
        for params in canonical_params:
            updated = await self._session.execute(
                text(
                    """
                    UPDATE mvp_characters
                    SET current_hp=:hp,max_hp=:max_hp
                    WHERE campaign_id=:c AND entity_id=:e
                    RETURNING entity_id
                    """
                ),
                {
                    "hp": params["hp"],
                    "max_hp": params["max_hp"],
                    "c": params["c"],
                    "e": params["e"],
                },
            )
            if updated.scalar_one_or_none() is None:
                raise InvalidCommitBundleError("Canonical更新対象が消失しました")
        await self._session.execute(
            text(
                "UPDATE campaigns SET state_version=:v,event_sequence=event_sequence+:n WHERE id=:c"
            ),
            {"v": version, "n": len(event_params), "c": bundle.campaign_id},
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
            {"v": version, "ni": _json(narration_input), "t": bundle.turn_id},
        )
        for action in action_params:
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
                    **action,
                    "c": bundle.campaign_id,
                    "t": bundle.turn_id,
                    "command": _json(action["command"]),
                    "result": _json(action["result"]),
                    "ruleset": campaign["ruleset_version"],
                },
            )
        for event in event_params:
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
                    **event,
                    "c": bundle.campaign_id,
                    "s": bundle.scene_id,
                    "t": bundle.turn_id,
                    "v": version,
                    "payload": _json(event["payload"]),
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
                RETURNING scene_id,actor_id,committed_state_version,created_at
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
        if choices:
            later_turn = await self._session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM turns
                        WHERE campaign_id=:c
                          AND actor_id=:a
                          AND (created_at,id) > (:created_at,:t)
                    )
                    """
                ),
                {
                    "c": campaign_id,
                    "a": row["actor_id"],
                    "created_at": row["created_at"],
                    "t": turn_id,
                },
            )
            if later_turn.scalar_one():
                return True
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
