"""Turn解決と描写を一回ずつ進めるApplication worker use case。"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from ai_rpg.application.ports import (
    ActionRecord,
    AuthorizationError,
    CanonicalSnapshot,
    ChoiceDraft,
    CommitBundle,
    FailureDisposition,
    LLMPhase,
    NarrationWorkItem,
    NarrativeCommit,
    PhaseDeadlineExceededError,
    ResolutionWorkItem,
    StateVersionConflictError,
    UnitOfWork,
)
from ai_rpg.application.routing import RuleBasedTurnRouter, TurnRouter
from ai_rpg.contracts import make_decision_types
from ai_rpg.contracts.context import (
    ContextFragment,
    MechanicalInput,
    NarrativeInput,
    OutputLimits,
)
from ai_rpg.contracts.llm_decisions import SkillCheckIntent
from ai_rpg.contracts.responses import MechanicalNarrationDraft, MechanicalNarrationInput
from ai_rpg.domain.commands import SkillCheckCommand
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import ResolvedAction
from ai_rpg.engine import MvpV1Ruleset
from ai_rpg.llm import CallBudgetExceeded
from ai_rpg.llm.structured import ProviderTransport, StructuredOutputAdapter, StructuredRequest


class ResolutionInputError(ValueError):
    """LLM提案をCanonicalな技能判定へ安全に対応付けられない。"""


@dataclass(frozen=True, slots=True)
class WorkerPhasePolicy:
    lease_seconds: int
    max_attempts: int
    deadline_seconds: int
    model_id: str
    request_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attemptsは1以上である必要があります")
        if self.request_timeout_seconds >= self.lease_seconds:
            raise ValueError("request timeoutはleaseより短くする必要があります")
        if self.deadline_seconds < self.lease_seconds:
            raise ValueError("phase deadlineはlease以上である必要があります")


UnitOfWorkFactory = Callable[[], UnitOfWork]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _stored_int(value: object) -> int:
    if type(value) is not int:
        raise ResolutionInputError("Canonical整数列の型が不正です")
    return value


async def _reserve_call(
    unit_of_work_factory: UnitOfWorkFactory,
    turn_id: UUID,
    phase: LLMPhase,
    worker_epoch: int,
) -> bool:
    if phase not in ("resolution", "narration"):
        raise ValueError("未対応のLLM phaseです")
    async with unit_of_work_factory() as unit_of_work:
        reserved = await unit_of_work.llm_calls.reserve(
            turn_id,
            phase=phase,
            worker_epoch=worker_epoch,
        )
        await unit_of_work.commit()
    return reserved


async def _record_failure(
    unit_of_work_factory: UnitOfWorkFactory,
    turn_id: UUID,
    phase: LLMPhase,
    worker_epoch: int,
    failure_code: str,
    max_attempts: int,
) -> FailureDisposition | None:
    async with unit_of_work_factory() as unit_of_work:
        disposition = await unit_of_work.llm_calls.record_failure(
            turn_id,
            phase=phase,
            worker_epoch=worker_epoch,
            failure_code=failure_code,
            max_attempts=max_attempts,
        )
        await unit_of_work.commit()
    return disposition


class SkillCheckResolutionWorker:
    """Fakeまたは実transportのIntentをEngineで技能判定しatomicに確定する。"""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        transport: ProviderTransport,
        ruleset: MvpV1Ruleset,
        policy: WorkerPhasePolicy,
        *,
        action_id_factory: Callable[[], UUID] = uuid4,
        choice_id_factory: Callable[[], UUID] = uuid4,
        router: TurnRouter | None = None,
        rng_source: Literal["secure", "seeded_test", "recorded_replay"] = "secure",
        rng_implementation_version: str = "mvp_v1",
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._transport = transport
        self._ruleset = ruleset
        self._policy = policy
        self._action_id_factory = action_id_factory
        self._choice_id_factory = choice_id_factory
        self._router = router or RuleBasedTurnRouter()
        self._rng_source = rng_source
        self._rng_implementation_version = rng_implementation_version

    async def run_once(self, turn_id: UUID | None = None) -> bool:
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await unit_of_work.turns.acquire_lease(
                turn_id,
                lease_seconds=self._policy.lease_seconds,
                max_attempts=self._policy.max_attempts,
                deadline_seconds=self._policy.deadline_seconds,
            )
            await unit_of_work.commit()
        if lease is None:
            return False

        async with self._unit_of_work_factory() as unit_of_work:
            snapshot = await unit_of_work.canonical.snapshot(lease.turn.campaign_id)
            work = await unit_of_work.turns.get_resolution_work(
                lease.turn.id, lease.turn.worker_epoch
            )
            await unit_of_work.commit()
        if work is None:
            return False
        if not work.actor_authorized:
            return await self._finalize_not_applied(
                work,
                "このActorを操作する権限を確認できません。入力をやり直してください。",
            )
        if lease.terminal_cleanup:
            return await self._handle_failure(work, "UNKNOWN")
        if work.expected_state_version != snapshot.state_version:
            return await self._finalize_not_applied(
                work,
                "状況が更新されたため判定を開始しませんでした。もう一度入力してください。",
            )

        route = work.route
        if route is None:
            route_decision = self._router.decide(work.player_text)
            async with self._unit_of_work_factory() as unit_of_work:
                recorded = await unit_of_work.turns.record_initial_route(
                    work.turn_id,
                    work.worker_epoch,
                    route_decision.route,
                    route_decision.rule_version,
                    route_decision.reason_codes,
                )
                if not recorded:
                    await unit_of_work.rollback()
                    return False
                await unit_of_work.commit()
            route = route_decision.route

        if route == "narrative":
            return await self._resolve_narrative(work, snapshot)
        return await self._resolve_mechanical(work, snapshot)

    async def _resolve_mechanical(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
    ) -> bool:

        _, decision_adapter = make_decision_types(work.max_actions)

        async def reserve() -> bool:
            return await _reserve_call(
                self._unit_of_work_factory,
                work.turn_id,
                "resolution",
                work.worker_epoch,
            )

        try:
            async with asyncio.timeout(self._policy.request_timeout_seconds):
                decision = await StructuredOutputAdapter(self._transport, reserve).generate(
                    StructuredRequest(
                        model_id=self._policy.model_id,
                        purpose="intent",
                        system_instruction=(
                            "登録済み情報だけを使い、数値結果を決めずに技能判定Intentを返す。"
                        ),
                        input_data=_json(
                            self._mechanical_input(work, snapshot).model_dump(mode="json")
                        ),
                        output_adapter=decision_adapter,
                    )
            )
            if decision.kind == "clarification_required":
                return await self._finalize_not_applied(work, decision.question)
            if decision.kind != "action_plan":
                raise ResolutionInputError("技能判定ActionPlanが必要です")

            records, resolved, public_state = self._resolve_actions(
                work, snapshot, decision.actions
            )
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ValidationError:
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        except ResolutionInputError:
            return await self._finalize_not_applied(
                work,
                "登録済みの技能判定として解決できません。行動を言い換えてください。",
            )
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")
        return await self._commit_mechanical(work, snapshot, records, resolved, public_state)

    async def _resolve_narrative(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
    ) -> bool:
        decision_adapter, _ = make_decision_types(work.max_actions)

        async def reserve() -> bool:
            return await _reserve_call(
                self._unit_of_work_factory,
                work.turn_id,
                "resolution",
                work.worker_epoch,
            )

        try:
            async with asyncio.timeout(self._policy.request_timeout_seconds):
                decision = await StructuredOutputAdapter(self._transport, reserve).generate(
                    StructuredRequest(
                        model_id=self._policy.model_id,
                        purpose="narrative",
                        system_instruction=(
                            "Canonical状態を変えず、登録済みの公開情報だけで応答する。"
                            "状態変更が必要ならresolution_requiredを返す。"
                        ),
                        input_data=_json(
                            self._narrative_input(work, snapshot).model_dump(mode="json")
                        ),
                        output_adapter=decision_adapter,
                    )
                )
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ValidationError:
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")

        if decision.kind == "clarification_required":
            return await self._finalize_not_applied(work, decision.question)
        if decision.kind == "narrative":
            choices = tuple(
                ChoiceDraft(self._choice_id_factory(), ordinal, choice.label)
                for ordinal, choice in enumerate(decision.choices, start=1)
            )
            return await self._commit_narrative(
                work, snapshot, decision.narration, choices
            )

        async with self._unit_of_work_factory() as unit_of_work:
            promoted = await unit_of_work.turns.promote_to_mechanical(
                work.turn_id, work.worker_epoch
            )
            if not promoted:
                await unit_of_work.rollback()
                return False
            await unit_of_work.commit()
        try:
            records, resolved, public_state = self._resolve_actions(
                work, snapshot, decision.actions
            )
        except ResolutionInputError:
            return await self._finalize_not_applied(
                work,
                "登録済みの技能判定として解決できません。行動を言い換えてください。",
            )
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")
        return await self._commit_mechanical(work, snapshot, records, resolved, public_state)

    async def _commit_narrative(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
        narration: str,
        choices: tuple[ChoiceDraft, ...],
    ) -> bool:
        commit = NarrativeCommit(
            campaign_id=work.campaign_id,
            scene_id=work.scene_id,
            turn_id=work.turn_id,
            worker_epoch=work.worker_epoch,
            base_state_version=snapshot.state_version,
            narration=narration,
            choices=choices,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.turns.commit_narrative(commit)
                await unit_of_work.commit()
        except AuthorizationError:
            return await self._finalize_not_applied(
                work,
                "Actorの操作権が更新されたため応答を確定しませんでした。"
                "もう一度入力してください。",
            )
        except StateVersionConflictError:
            return await self._finalize_not_applied(
                work,
                "状況が更新されたため応答を確定しませんでした。もう一度入力してください。",
            )
        except PhaseDeadlineExceededError:
            return await self._handle_failure(work, "UNKNOWN")
        except Exception as commit_error:
            if await self._commit_is_visible(work, require_completed_narration=True):
                return True
            raise commit_error
        return True

    async def _commit_mechanical(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
        records: tuple[ActionRecord, ...],
        resolved: list[ResolvedAction],
        public_state: list[ContextFragment],
    ) -> bool:
        narration_input = MechanicalNarrationInput(
            player_text=work.player_text,
            committed_state_version=snapshot.state_version,
            resolved_actions=resolved,
            public_state_after=public_state,
            allowed_entity_refs=[],
            output_limits=OutputLimits(max_actions=work.max_actions, max_choices=5),
        )
        bundle = CommitBundle(
            campaign_id=work.campaign_id,
            scene_id=work.scene_id,
            turn_id=work.turn_id,
            worker_epoch=work.worker_epoch,
            base_state_version=snapshot.state_version,
            actions=records,
            narration_input=narration_input,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.turns.commit_resolution(bundle)
                await unit_of_work.commit()
        except AuthorizationError:
            return await self._finalize_not_applied(
                work,
                "Actorの操作権が更新されたため行動を確定しませんでした。"
                "もう一度入力してください。",
            )
        except StateVersionConflictError:
            return await self._finalize_not_applied(
                work,
                "状況が更新されたため判定を確定しませんでした。もう一度入力してください。",
            )
        except PhaseDeadlineExceededError:
            return await self._handle_failure(work, "UNKNOWN")
        except Exception as commit_error:
            if await self._commit_is_visible(work, require_completed_narration=False):
                return True
            raise commit_error
        return True

    async def _commit_is_visible(
        self,
        work: ResolutionWorkItem,
        *,
        require_completed_narration: bool,
    ) -> bool:
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.get_response(
                work.campaign_id, work.turn_id
            )
            await unit_of_work.rollback()
        if response is None or response.resolution_status != "committed":
            return False
        if require_completed_narration:
            return response.route == "narrative" and response.narration_status == "completed"
        return response.route == "mechanical"

    def _narrative_input(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> NarrativeInput:
        scene_view, pc_view = self._public_context(work, snapshot)
        return NarrativeInput(
            player_text=work.player_text,
            scene_view=scene_view,
            pc_view=pc_view,
            recent_messages=[],
            allowed_entity_refs=[],
            output_limits=OutputLimits(max_actions=work.max_actions, max_choices=5),
        )

    def _mechanical_input(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> MechanicalInput:
        scene_view, pc_view = self._public_context(work, snapshot)
        supported_skills = sorted(
            {
                str(check["skill_ref"])
                for check in snapshot.skill_checks
                if check["scene_id"] == work.scene_id
            }
        )
        return MechanicalInput(
            player_text=work.player_text,
            scene_view=scene_view,
            pc_view=pc_view,
            recent_messages=[],
            allowed_entity_refs=[],
            output_limits=OutputLimits(max_actions=work.max_actions, max_choices=5),
            supported_action_types=["skill_check"],
            supported_skill_refs=supported_skills,
        )

    def _public_context(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> tuple[ContextFragment, ContextFragment]:
        modifiers = [
            f"{row['skill_ref']}:{_stored_int(row['modifier']):+d}"
            for row in snapshot.skills
            if row["character_id"] == work.actor_id
        ]
        return (
            ContextFragment(
                source="active_scene",
                trust_level="trusted",
                access_scope="public",
                content="現在のSceneに公開済みの追加情報はない。",
            ),
            ContextFragment(
                source="active_pc",
                trust_level="trusted",
                access_scope="actor_private",
                content=(
                    "操作中PC。技能補正: " + ", ".join(modifiers)
                    if modifiers
                    else "操作中PC。公開可能な技能補正はない。"
                ),
            ),
        )

    async def _handle_failure(
        self, work: ResolutionWorkItem, failure_code: str
    ) -> bool:
        disposition = await _record_failure(
            self._unit_of_work_factory,
            work.turn_id,
            "resolution",
            work.worker_epoch,
            failure_code,
            self._policy.max_attempts,
        )
        if disposition is None:
            return False
        if disposition == "retry":
            return True
        return await self._save_terminal_narration(
            work.turn_id,
            "行動を確定できませんでした。入力を言い換えてください。",
            failure_code,
        )

    async def _finalize_not_applied(
        self, work: ResolutionWorkItem, question: str
    ) -> bool:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                finalized = await unit_of_work.turns.finalize_not_applied(
                    work.turn_id, work.worker_epoch, question
                )
                if not finalized:
                    await unit_of_work.rollback()
                    return False
                await unit_of_work.commit()
        except PhaseDeadlineExceededError:
            return await self._handle_failure(work, "UNKNOWN")
        return True

    async def _save_terminal_narration(
        self,
        turn_id: UUID,
        narration: str,
        fallback_reason: str | None,
    ) -> bool:
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await unit_of_work.narration.acquire_lease(
                turn_id,
                lease_seconds=self._policy.lease_seconds,
                max_attempts=self._policy.max_attempts,
                deadline_seconds=self._policy.deadline_seconds,
            )
            await unit_of_work.commit()
        if lease is None:
            return False
        async with self._unit_of_work_factory() as unit_of_work:
            saved = await unit_of_work.narration.save_conditionally(
                lease.campaign_id,
                lease.turn_id,
                lease.worker_epoch,
                narration,
                (),
                fallback_reason=fallback_reason,
            )
            if not saved:
                await unit_of_work.rollback()
                return False
            await unit_of_work.commit()
        return True

    def _resolve_actions(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
        intents: list[object],
    ) -> tuple[
        tuple[ActionRecord, ...],
        list[ResolvedAction],
        list[ContextFragment],
    ]:
        character = next(
            (
                row
                for row in snapshot.characters
                if row["entity_id"] == work.actor_id
            ),
            None,
        )
        if character is None:
            raise ResolutionInputError("actorのCanonical状態が存在しません")
        actor = CharacterState(
            id=work.actor_id,
            current_hp=_stored_int(character["current_hp"]),
            max_hp=_stored_int(character["max_hp"]),
            defense=_stored_int(character["defense"]),
            attack_bonus=_stored_int(character["attack_bonus"]),
        )

        records: list[ActionRecord] = []
        resolved: list[ResolvedAction] = []
        public_state: list[ContextFragment] = []
        draw_index = 0
        for ordinal, raw_intent in enumerate(intents, start=1):
            if not isinstance(raw_intent, SkillCheckIntent):
                raise ResolutionInputError("最初の受入経路は技能判定だけを扱います")
            if raw_intent.target_ref is not None:
                raise ResolutionInputError("未解決のtarget_refは使用できません")
            checks = [
                row
                for row in snapshot.skill_checks
                if row["scene_id"] == work.scene_id
                and row["skill_ref"] == raw_intent.skill_ref
                and row["target_id"] is None
            ]
            if len(checks) != 1:
                raise ResolutionInputError("登録済みScene技能判定を一意に解決できません")
            check = checks[0]
            modifier_row = next(
                (
                    row
                    for row in snapshot.skills
                    if row["character_id"] == work.actor_id
                    and row["skill_ref"] == raw_intent.skill_ref
                ),
                None,
            )
            if modifier_row is None:
                raise ResolutionInputError("actorに登録済み技能補正がありません")
            try:
                difficulty_class = self._ruleset.difficulty_class(
                    str(check["difficulty"])
                )
            except ValueError as error:
                raise ResolutionInputError(str(error)) from error

            command = SkillCheckCommand(
                kind="skill_check",
                action_id=self._action_id_factory(),
                campaign_id=work.campaign_id,
                turn_id=work.turn_id,
                actor_id=work.actor_id,
                ordinal=ordinal,
                skill_ref=raw_intent.skill_ref,
                objective=raw_intent.objective,
                target_id=None,
                modifier=_stored_int(modifier_row["modifier"]),
                difficulty_class=difficulty_class,
            )
            try:
                result = self._ruleset.resolve_skill_check(command, actor)
            except ValueError as error:
                raise ResolutionInputError(str(error)) from error
            rng = tuple(
                RNGMetadata(
                    source=self._rng_source,
                    implementation_version=self._rng_implementation_version,
                    draw_index=draw_index + offset,
                )
                for offset, _roll in enumerate(result.dice)
            )
            draw_index += len(result.dice)
            records.append(ActionRecord(command=command, result=result, rng=rng))
            resolved.append(
                ResolvedAction(
                    action_id=command.action_id,
                    ordinal=command.ordinal,
                    result=result,
                )
            )
            if result.outcome == "success":
                public_state.append(
                    ContextFragment(
                        source=f"scene_check:{check['check_ref']}",
                        trust_level="trusted",
                        access_scope="public",
                        content=str(check["public_description"]),
                    )
                )
        return tuple(records), resolved, public_state


class NarrationWorker:
    """保存済み公開snapshotだけをFakeまたは実transportへ渡して描写を確定する。"""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        transport: ProviderTransport,
        policy: WorkerPhasePolicy,
        *,
        choice_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._transport = transport
        self._policy = policy
        self._choice_id_factory = choice_id_factory

    async def run_once(self, turn_id: UUID | None = None) -> bool:
        async with self._unit_of_work_factory() as unit_of_work:
            lease = await unit_of_work.narration.acquire_lease(
                turn_id,
                lease_seconds=self._policy.lease_seconds,
                max_attempts=self._policy.max_attempts,
                deadline_seconds=self._policy.deadline_seconds,
            )
            await unit_of_work.commit()
        if lease is None:
            return False

        async with self._unit_of_work_factory() as unit_of_work:
            work = await unit_of_work.narration.get_work(
                lease.turn_id, lease.worker_epoch
            )
            await unit_of_work.commit()
        if work is None:
            return False
        if work.narration_input is None:
            return await self._save_fallback(
                work,
                "UNKNOWN",
                "行動を確定できませんでした。入力を言い換えてください。",
            )
        if lease.terminal_cleanup:
            return await self._handle_failure(work, "UNKNOWN")

        async def reserve() -> bool:
            return await _reserve_call(
                self._unit_of_work_factory,
                work.turn_id,
                "narration",
                work.worker_epoch,
            )

        try:
            async with asyncio.timeout(self._policy.request_timeout_seconds):
                draft = await StructuredOutputAdapter(self._transport, reserve).generate(
                    StructuredRequest(
                        model_id=self._policy.model_id,
                        purpose="result_narration",
                        system_instruction="保存済みの確定結果だけを描写し、新しいゲーム事実を追加しない。",
                        input_data=_json(work.narration_input.model_dump(mode="json")),
                        output_adapter=TypeAdapter(MechanicalNarrationDraft),
                    )
                )
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ValidationError:
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        choices = tuple(
            ChoiceDraft(self._choice_id_factory(), ordinal, choice.label)
            for ordinal, choice in enumerate(draft.choices, start=1)
        )
        async with self._unit_of_work_factory() as unit_of_work:
            saved = await unit_of_work.narration.save_conditionally(
                work.campaign_id,
                work.turn_id,
                work.worker_epoch,
                draft.narration,
                choices,
            )
            if not saved:
                await unit_of_work.rollback()
                return await self._handle_failure(work, "UNKNOWN")
            await unit_of_work.commit()
        return True

    async def _handle_failure(
        self, narration_work: NarrationWorkItem, failure_code: str
    ) -> bool:
        disposition = await _record_failure(
            self._unit_of_work_factory,
            narration_work.turn_id,
            "narration",
            narration_work.worker_epoch,
            failure_code,
            self._policy.max_attempts,
        )
        if disposition is None:
            return False
        if disposition == "retry":
            return True
        return await self._save_fallback(narration_work, failure_code)

    async def _save_fallback(
        self,
        narration_work: NarrationWorkItem,
        failure_code: str,
        narration: str = "判定結果は保存されましたが、描写を生成できませんでした。",
    ) -> bool:
        async with self._unit_of_work_factory() as unit_of_work:
            saved = await unit_of_work.narration.save_conditionally(
                narration_work.campaign_id,
                narration_work.turn_id,
                narration_work.worker_epoch,
                narration,
                (),
                fallback_reason=failure_code,
            )
            if not saved:
                await unit_of_work.rollback()
                return False
            await unit_of_work.commit()
        return True
