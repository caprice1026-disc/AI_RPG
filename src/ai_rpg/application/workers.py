"""Turn解決と描写を一回ずつ進めるApplication worker use case。"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from ai_rpg.application.combat import resolve_enemy_reaction
from ai_rpg.application.entity_refs import EntityRefMap
from ai_rpg.application.narration_grounding import (
    NarrationGroundingError,
    validate_mechanical_narration,
)
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
    ScenarioRunSnapshot,
    StateVersionConflictError,
    UnitOfWork,
)
from ai_rpg.application.ports.llm import (
    ProviderHTTPError,
    ProviderOutputError,
    ProviderRefusalError,
    ResolutionLLM,
    ResultNarrator,
)
from ai_rpg.application.ports.repositories import ScenarioProgressUpdate
from ai_rpg.application.routing import RouteDecision, RuleBasedTurnRouter, TurnRouter
from ai_rpg.application.scenarios import (
    ScenarioActionBinding,
    ScenarioActionUnavailableError,
    ScenarioProgressor,
    ScenarioPublicContext,
)
from ai_rpg.contracts.context import (
    ContextFragment,
    EntityRef,
    MechanicalInput,
    NarrativeInput,
    OpenActionOptions,
    OutputLimits,
)
from ai_rpg.contracts.llm_decisions import (
    ActionIntent,
    AttackIntent,
    OpenActionIntent,
    ScenarioActionIntent,
    SkillCheckIntent,
    UseItemIntent,
)
from ai_rpg.contracts.responses import MechanicalNarrationInput
from ai_rpg.domain.commands import (
    AttackCommand,
    OpenActionCommand,
    ScenarioActionCommand,
    SkillCheckCommand,
    UseItemCommand,
)
from ai_rpg.domain.events import RNGMetadata
from ai_rpg.domain.models import CharacterState
from ai_rpg.domain.results import (
    AppliedResult,
    DamageApplied,
    HealingApplied,
    ItemConsumed,
    NotApplicableResult,
    ResolvedAction,
    ResolvedEnemyReaction,
)
from ai_rpg.engine import MvpV1Ruleset
from ai_rpg.engine.ruleset import MvpV2Ruleset
from ai_rpg.llm.budget import CallBudgetExceeded
from ai_rpg.scenarios import AttackScenarioAction, DirectScenarioAction, SkillScenarioAction


class ResolutionInputError(ValueError):
    """LLM提案をCanonicalなActionへ安全に対応付けられない。"""


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


@dataclass(frozen=True, slots=True)
class _ActionPreflight:
    scenario_bindings: list[ScenarioActionBinding | None]
    characters: dict[UUID, CharacterState]
    refs: EntityRefMap
    id_to_ref: dict[UUID, str]
    weapons: dict[UUID, Mapping[str, object]]
    quantities: dict[tuple[UUID, UUID], int]
    skill_data: dict[str, tuple[Mapping[str, object], int]]


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
        llm: ResolutionLLM,
        ruleset: MvpV1Ruleset,
        policy: WorkerPhasePolicy,
        *,
        action_id_factory: Callable[[], UUID] = uuid4,
        choice_id_factory: Callable[[], UUID] = uuid4,
        router: TurnRouter | None = None,
        scenario_progressor: ScenarioProgressor | None = None,
        recent_messages_limit: int = 20,
        rng_source: Literal["secure", "seeded_test", "recorded_replay"] = "secure",
        rng_implementation_version: str = "mvp_v1",
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._llm = llm
        self._ruleset = ruleset
        self._policy = policy
        self._action_id_factory = action_id_factory
        self._choice_id_factory = choice_id_factory
        self._router = router or RuleBasedTurnRouter()
        self._scenario_progressor = scenario_progressor
        if not 0 <= recent_messages_limit <= 100:
            raise ValueError("recent_messages_limitは0から100の範囲で指定してください")
        self._recent_messages_limit = recent_messages_limit
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
            snapshot = await unit_of_work.canonical.snapshot(
                lease.turn.campaign_id, lease.turn.scene_id
            )
            work = await unit_of_work.turns.get_resolution_work(
                lease.turn.id,
                lease.turn.worker_epoch,
                recent_messages_limit=self._recent_messages_limit,
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
            route_decision = (
                RouteDecision("mechanical", "registered_action_v1", ("registered_action",))
                if work.selected_action_ref is not None or work.confirmed_proposal_id is not None
                else self._router.decide(work.player_text)
            )
            if (
                snapshot.scenario_run is not None
                and snapshot.scenario_run.scenario_version >= 3
                and route_decision.route == "narrative"
                and route_decision.reason_codes == ("narrative_or_fallback",)
            ):
                route_decision = RouteDecision("mechanical", "bounded_open_v1", ("free_action",))
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

        try:
            intents: Sequence[object]
            if work.confirmed_proposal_id is not None:
                async with self._unit_of_work_factory() as unit_of_work:
                    proposal = await unit_of_work.turns.get_confirmed_proposal(work)
                    await unit_of_work.commit()
                if proposal is None:
                    raise ResolutionInputError("Confirmed action is unavailable")
                intents = [proposal]
            elif work.selected_action_ref is not None:
                intents = [self._registered_intent(work, snapshot)]
            else:
                context = self._mechanical_input(work, snapshot)
                async with asyncio.timeout(self._policy.request_timeout_seconds):
                    if not await _reserve_call(
                        self._unit_of_work_factory, work.turn_id, "resolution", work.worker_epoch
                    ):
                        raise CallBudgetExceeded("LLM呼び出し予算を使い切りました")
                    decision = await self._llm.extract_intent(
                        context, model_id=self._policy.model_id
                    )
                if decision.kind == "clarification_required":
                    return await self._finalize_not_applied(work, decision.question)
                if decision.kind != "action_plan":
                    raise ResolutionInputError("ActionPlanが必要です")
                intents = decision.actions

            preflight = self._preflight_actions(work, snapshot, intents)
            if work.confirmed_proposal_id is None:
                risk = self._risk_for_actions(intents, snapshot)
                if risk is not None:
                    return await self._preview_risk(work, risk[0], risk[1])
            records, resolved, public_state, scenario_update = self._resolve_actions(
                work, snapshot, intents, preflight=preflight
            )
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ProviderRefusalError:
            return await self._handle_failure(work, "MODEL_REFUSAL")
        except (ValidationError, ProviderOutputError):
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        except ProviderHTTPError:
            return await self._handle_failure(work, "UNKNOWN")
        except ResolutionInputError:
            return await self._finalize_not_applied(
                work,
                "その方法は今の状況では確定できません。場所や目的を確認して言い換えてください。",
            )
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")
        return await self._commit_mechanical(
            work, snapshot, records, resolved, public_state, scenario_update
        )

    def _registered_intent(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> AttackIntent | SkillCheckIntent | ScenarioActionIntent:
        if snapshot.scenario_run is None or self._scenario_progressor is None:
            raise ResolutionInputError("Scenario runがありません")
        assert work.selected_action_ref is not None
        try:
            binding = self._scenario_progressor.bind_registered_action(
                snapshot.scenario_run, work.selected_action_ref
            )
        except ScenarioActionUnavailableError as error:
            raise ResolutionInputError(str(error)) from error
        if isinstance(binding, DirectScenarioAction):
            return ScenarioActionIntent(kind="scenario_action", action_ref=binding.action_ref)
        if isinstance(binding, SkillScenarioAction):
            return SkillCheckIntent(
                kind="skill_check",
                skill_ref=binding.skill_ref,
                objective=binding.label,
                target_ref=None,
            )
        assert isinstance(binding, AttackScenarioAction)
        equipped = {
            row["item_id"]
            for row in snapshot.inventory
            if row["owner_id"] == work.actor_id
            and row["equipped"]
            and _stored_int(row["quantity"]) > 0
        }
        weapons = {row["entity_id"] for row in snapshot.equipment}
        weapon_refs = sorted(
            str(row["ref"])
            for row in snapshot.entities
            if row["id"] in equipped & weapons
            and row["ref"] is not None
            and row["archived_at"] is None
        )
        return AttackIntent(
            kind="attack",
            target_ref=binding.target_ref,
            weapon_ref=weapon_refs[0] if weapon_refs else None,
        )

    async def _resolve_narrative(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
    ) -> bool:
        try:
            context = self._narrative_input(work, snapshot)
            async with asyncio.timeout(self._policy.request_timeout_seconds):
                if not await _reserve_call(
                    self._unit_of_work_factory, work.turn_id, "resolution", work.worker_epoch
                ):
                    raise CallBudgetExceeded("LLM呼び出し予算を使い切りました")
                decision = await self._llm.generate_narrative(
                    context, model_id=self._policy.model_id
                )
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ProviderRefusalError:
            return await self._handle_failure(work, "MODEL_REFUSAL")
        except (ValidationError, ProviderOutputError):
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        except ProviderHTTPError:
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
            return await self._commit_narrative(work, snapshot, decision.narration, choices)

        try:
            preflight = self._preflight_actions(work, snapshot, decision.actions)
            risk = self._risk_for_actions(decision.actions, snapshot)
            if risk is not None:
                return await self._preview_risk(work, risk[0], risk[1])
        except ResolutionInputError:
            return await self._finalize_not_applied(
                work,
                "その方法は今の状況では確定できません。場所や目的を確認して言い換えてください。",
            )
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")
        async with self._unit_of_work_factory() as unit_of_work:
            promoted = await unit_of_work.turns.promote_to_mechanical(
                work.turn_id, work.worker_epoch
            )
            if not promoted:
                await unit_of_work.rollback()
                return False
            await unit_of_work.commit()
        try:
            records, resolved, public_state, scenario_update = self._resolve_actions(
                work, snapshot, decision.actions, preflight=preflight
            )
        except ResolutionInputError:
            return await self._finalize_not_applied(
                work,
                "その方法は今の状況では確定できません。場所や目的を確認して言い換えてください。",
            )
        except Exception:
            return await self._handle_failure(work, "UNKNOWN")
        return await self._commit_mechanical(
            work, snapshot, records, resolved, public_state, scenario_update
        )

    def _risk_for_actions(
        self, intents: Sequence[object], snapshot: CanonicalSnapshot,
    ) -> tuple[ActionIntent, str] | None:
        run = snapshot.scenario_run
        if run is None or run.scenario_version < 3 or self._scenario_progressor is None:
            return None
        if len(intents) != 1:
            return None  # _preflight_actions rejects multi-action v3 plans.
        intent = intents[0]
        if not isinstance(intent, (
            OpenActionIntent, ScenarioActionIntent, AttackIntent, SkillCheckIntent, UseItemIntent,
        )):
            return None
        risks: list[str] = []
        can_react = True
        if isinstance(intent, OpenActionIntent):
            effects = [intent.success]
            if intent.failure is not None:
                effects.append(intent.failure)
            can_react = any(
                effect.next_scene_ref is None and effect.ending_ref is None
                for effect in effects
            )
            reaches_ending = intent.success.ending_ref is not None or (
                intent.failure is not None and intent.failure.ending_ref is not None
            )
            loses_reward = "costly" in intent.success.add_flags or (
                intent.failure is not None and "costly" in intent.failure.add_flags
            )
            if reaches_ending or loses_reward:
                risks.append(
                    "この行動は冒険の結末を確定する可能性があります。" if reaches_ending
                    else "この行動は報酬を減らす代償が残る可能性があります。"
                )
        elif isinstance(intent, ScenarioActionIntent):
            binding = self._scenario_progressor.bind_scenario_action(run, intent.action_ref)
            if binding is not None:
                can_react = (
                    binding.success.next_scene_ref is None
                    and binding.success.ending_ref is None
                )
                if binding.success.ending_ref is not None:
                    risks.append("この行動で冒険の結末が確定します。")
        elif isinstance(intent, SkillCheckIntent):
            skill_binding = self._scenario_progressor.bind_skill_check(run, intent.skill_ref)
            if skill_binding is not None:
                can_react = any(
                    effect.next_scene_ref is None and effect.ending_ref is None
                    for effect in (skill_binding.success, skill_binding.failure)
                )
                if (skill_binding.success.ending_ref is not None
                        or skill_binding.failure.ending_ref is not None):
                    risks.append("この判定で冒険の結末が確定する可能性があります。")
        combat = self._scenario_progressor.scene_for(run).combat
        if can_react and combat is not None and (
            combat.started_flag in run.flags or isinstance(intent, AttackIntent)
        ):
            risks.insert(0, "敵の反撃でHPを失う可能性があります。")
        return (intent, " ".join(risks)[:500]) if risks else None

    async def _preview_risk(
        self, work: ResolutionWorkItem, proposal: ActionIntent, risk_text: str,
    ) -> bool:
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.turns.save_risk_proposal(work, proposal, risk_text)
                saved = await unit_of_work.turns.finalize_not_applied(
                    work.turn_id, work.worker_epoch,
                    f"実行前に確認してください: {risk_text}",
                )
                if not saved:
                    await unit_of_work.rollback()
                    return False
                await unit_of_work.commit()
        except (StateVersionConflictError, AuthorizationError):
            return await self._finalize_not_applied(
                work, "状況が変わったため危険な行動を確定しませんでした。",
            )
        except PhaseDeadlineExceededError:
            return await self._handle_failure(work, "UNKNOWN")
        return True

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
                "Actorの操作権が更新されたため応答を確定しませんでした。もう一度入力してください。",
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
        scenario_update: ScenarioProgressUpdate | None,
    ) -> bool:
        combat = resolve_enemy_reaction(
            work,
            snapshot,
            records,
            scenario_update,
            ruleset=self._ruleset,
            progressor=self._scenario_progressor,
            reaction_id_factory=self._action_id_factory,
            rng_source=self._rng_source,
            rng_implementation_version=self._rng_implementation_version,
        )
        if combat.scenario_update != scenario_update and combat.scenario_update is not None:
            assert snapshot.scenario_run is not None
            public_state.append(
                self._scenario_public_state_after(snapshot.scenario_run, combat.scenario_update)
            )
        scenario_update = combat.scenario_update
        for reaction in combat.reactions:
            for change in reaction.result.state_changes:
                if isinstance(change, DamageApplied):
                    actor_ref = next(
                        str(e["ref"]) for e in snapshot.entities if e["id"] == work.actor_id
                    )
                    public_state.append(
                        ContextFragment(
                            source=f"entity:{actor_ref}",
                            trust_level="derived",
                            access_scope="public",
                            content=f"敵の反撃後の @{actor_ref} HP {change.hp_after}",
                        )
                    )
        narration_reactions = [
            ResolvedEnemyReaction(
                reaction_id=r.command.action_id,
                actor_id=r.command.actor_id,
                target_id=r.command.target_id,
                result=r.result,
            )
            for r in combat.reactions
        ]
        results = [a.result for a in records] + [r.result for r in combat.reactions]
        state_changed = scenario_update is not None or any(
            isinstance(result, AppliedResult)
            and any(
                isinstance(change, ItemConsumed)
                or (
                    isinstance(change, (DamageApplied, HealingApplied))
                    and change.hp_before != change.hp_after
                )
                for change in result.state_changes
            )
            for result in results
        )
        narration_input = MechanicalNarrationInput(
            player_text=work.player_text,
            committed_state_version=snapshot.state_version + int(state_changed),
            resolved_actions=resolved,
            enemy_reactions=narration_reactions,
            public_state_after=public_state,
            allowed_entity_refs=self._allowed_entity_refs(work, snapshot),
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
            scenario_update=scenario_update,
            enemy_reactions=combat.reactions,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                await unit_of_work.turns.commit_resolution(bundle)
                await unit_of_work.commit()
        except AuthorizationError:
            return await self._finalize_not_applied(
                work,
                "Actorの操作権が更新されたため行動を確定しませんでした。もう一度入力してください。",
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
            response = await unit_of_work.turns.get_response(work.campaign_id, work.turn_id)
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
            recent_messages=self._recent_context(work),
            allowed_entity_refs=self._allowed_entity_refs(work, snapshot),
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
        supported_actions: list[
            Literal["attack", "skill_check", "use_item", "scenario_action", "open_action"]
        ] = []
        if self._attackable_entity_ids(work, snapshot):
            supported_actions.append("attack")
        if supported_skills:
            supported_actions.append("skill_check")
        if any(
            row["owner_id"] == work.actor_id and _stored_int(row["quantity"]) > 0
            for row in snapshot.inventory
        ):
            supported_actions.append("use_item")
        if snapshot.scenario_run is not None:
            if snapshot.scenario_run.scenario_version >= 3:
                supported_actions.append("open_action")
            scenario_context = self._scenario_context(snapshot)
            if any(
                self._scenario_progressor is not None
                and self._scenario_progressor.bind_scenario_action(
                    snapshot.scenario_run, action_ref
                )
                is not None
                for action_ref, _label in scenario_context.available_actions
            ):
                supported_actions.append("scenario_action")
        open_options = None
        if (snapshot.scenario_run is not None and self._scenario_progressor is not None
                and snapshot.scenario_run.scenario_version >= 3):
            definition = self._scenario_progressor.definition_for(snapshot.scenario_run)
            scene = self._scenario_progressor.scene_for(snapshot.scenario_run)
            open_options = OpenActionOptions(
                current_scene_ref=scene.scene_ref,
                destination_refs=list(scene.open_destinations),
                allowed_flag_refs=list(scene.open_flags),
                ending_refs=[ending.ending_ref for ending in definition.endings],
            )
        return MechanicalInput(
            player_text=work.player_text,
            scene_view=scene_view,
            pc_view=pc_view,
            recent_messages=self._recent_context(work),
            allowed_entity_refs=self._allowed_entity_refs(work, snapshot),
            output_limits=OutputLimits(max_actions=work.max_actions, max_choices=5),
            supported_action_types=supported_actions,
            supported_skill_refs=supported_skills,
            open_action_options=open_options,
        )

    @staticmethod
    def _visible_entity_ids(work: ResolutionWorkItem, snapshot: CanonicalSnapshot) -> set[UUID]:
        scene_public = {
            UUID(str(row["entity_id"])) for row in snapshot.scene_entities if bool(row["is_public"])
        }
        owned = {
            UUID(str(row["item_id"]))
            for row in snapshot.inventory
            if UUID(str(row["owner_id"])) == work.actor_id
        }
        return scene_public | owned | {work.actor_id}

    @staticmethod
    def _attackable_entity_ids(work: ResolutionWorkItem, snapshot: CanonicalSnapshot) -> set[UUID]:
        reachable = {
            UUID(str(row["entity_id"]))
            for row in snapshot.scene_entities
            if bool(row["is_public"]) and bool(row["is_attack_reachable"])
        }
        characters = {UUID(str(row["entity_id"])) for row in snapshot.characters}
        active_refs = {
            UUID(str(row["id"]))
            for row in snapshot.entities
            if row["ref"] is not None and row["archived_at"] is None
        }
        return (reachable & characters & active_refs) - {work.actor_id}

    def _allowed_entity_refs(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> list[EntityRef]:
        visible = self._visible_entity_ids(work, snapshot)
        refs = [
            EntityRef.model_validate(
                {
                    "ref": row["ref"],
                    "label": row["label"],
                    "entity_kind": row["kind"],
                }
            )
            for row in snapshot.entities
            if row["ref"] is not None
            and row["label"] is not None
            and row["archived_at"] is None
            and UUID(str(row["id"])) in visible
        ]
        return sorted(refs, key=lambda entity: entity.ref)

    @staticmethod
    def _recent_context(work: ResolutionWorkItem) -> list[ContextFragment]:
        trust = {
            "recent_player": "untrusted",
            "recent_action_result": "derived",
            "recent_gm": "derived",
        }
        return [
            ContextFragment(
                source=message.source,
                trust_level=trust[message.source],
                access_scope="public",
                content=message.content,
            )
            for message in work.recent_messages
        ]

    def _public_context(
        self, work: ResolutionWorkItem, snapshot: CanonicalSnapshot
    ) -> tuple[ContextFragment, ContextFragment]:
        modifiers = {
            str(row["skill_ref"]): _stored_int(row["modifier"])
            for row in snapshot.skills
            if row["character_id"] == work.actor_id
        }
        actor = next(
            (row for row in snapshot.characters if row["entity_id"] == work.actor_id), None
        )
        public_items = {
            row["id"]: row
            for row in snapshot.entities
            if row["ref"] is not None and row["archived_at"] is None
        }
        inventory = [
            {
                "item_ref": public_items[row["item_id"]]["ref"],
                "name": public_items[row["item_id"]]["label"],
                "quantity": _stored_int(row["quantity"]),
                "equipped": bool(row["equipped"]),
            }
            for row in snapshot.inventory
            if row["owner_id"] == work.actor_id and row["item_id"] in public_items
        ]
        scenario_context = (
            None if snapshot.scenario_run is None else self._scenario_context(snapshot)
        )
        world_context: dict[str, object] = {}
        if snapshot.scenario_run is not None and self._scenario_progressor is not None:
            world = self._scenario_progressor.definition_for(snapshot.scenario_run).world
            if world is not None:
                world_context = {
                    "region": world.region_name,
                    "boundary": world.boundary,
                    "protected_facts": [
                        fact.statement for fact in world.protected_facts
                        if fact.scene_ref is None or any(
                            runtime.status == "active"
                            and scenario_scene.scene_ref == fact.scene_ref
                            and runtime.sequence == scenario_scene.sequence
                            for runtime in snapshot.scenario_run.scenes
                            for scenario_scene in self._scenario_progressor.definition_for(
                                snapshot.scenario_run
                            ).scenes
                        )
                    ],
                }
        scene_payload: dict[str, object] | None = None
        if scenario_context is not None:
            scene_payload = {
                "scene_title": scenario_context.scene_title,
                "scene_description": scenario_context.scene_description,
                "objective": scenario_context.objective,
                "discovered_facts": scenario_context.discovered_facts,
                "available_actions": [
                    asdict(action) for action in scenario_context.action_details
                ],
                "npc_notes": scenario_context.npc_notes,
            }
            if snapshot.scenario_run is not None and snapshot.scenario_run.scenario_version >= 3:
                scene_payload.update({
                    "generated_facts": [
                        asdict(fact) for fact in scenario_context.generated_facts
                    ],
                    "world": world_context,
                    "elapsed_actions": snapshot.scenario_run.elapsed_actions,
                    "alert_level": snapshot.scenario_run.alert_level,
                })
        pc_payload: dict[str, object] = {
            "current_hp": None if actor is None else actor["current_hp"],
            "max_hp": None if actor is None else actor["max_hp"],
            "skills": modifiers,
            "inventory": inventory,
        }
        if snapshot.scenario_run is not None and snapshot.scenario_run.scenario_version >= 3:
            pc_payload["abilities"] = [
                dict(row) for row in snapshot.abilities if row["character_id"] == work.actor_id
            ]
        scene_view = ContextFragment(
            source="active_scene",
            trust_level="trusted",
            access_scope="public",
            content=(
                "現在のSceneに公開済みの追加情報はない。"
                if scene_payload is None else _json(scene_payload)
            ),
        )
        return (
            scene_view,
            ContextFragment(
                source="active_pc",
                trust_level="trusted",
                access_scope="actor_private",
                content=_json(pc_payload),
            ),
        )

    def _scenario_context(self, snapshot: CanonicalSnapshot) -> ScenarioPublicContext:
        if snapshot.scenario_run is None or self._scenario_progressor is None:
            raise ResolutionInputError("Scenario進行componentが設定されていません")
        return self._scenario_progressor.public_context_for(snapshot.scenario_run)

    async def _handle_failure(self, work: ResolutionWorkItem, failure_code: str) -> bool:
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

    async def _finalize_not_applied(self, work: ResolutionWorkItem, question: str) -> bool:
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

    def _preflight_actions(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
        intents: Sequence[object],
    ) -> _ActionPreflight:
        scenario_bindings = self._scenario_bindings(snapshot, intents)
        characters = {
            UUID(str(row["entity_id"])): CharacterState(
                id=UUID(str(row["entity_id"])),
                current_hp=_stored_int(row["current_hp"]),
                max_hp=_stored_int(row["max_hp"]),
                defense=_stored_int(row["defense"]),
                attack_bonus=_stored_int(row["attack_bonus"]),
            )
            for row in snapshot.characters
        }
        actor = characters.get(work.actor_id)
        if actor is None:
            raise ResolutionInputError("actorのCanonical状態が存在しません")
        if actor.current_hp == 0:
            raise ResolutionInputError("行動不能なactorです")
        allowed_refs = {entity.ref for entity in self._allowed_entity_refs(work, snapshot)}
        ref_rows = {
            str(row["ref"]): UUID(str(row["id"]))
            for row in snapshot.entities
            if row["ref"] in allowed_refs
        }
        refs = EntityRefMap(ref_rows)
        id_to_ref = {entity_id: ref for ref, entity_id in ref_rows.items()}
        weapons = {UUID(str(row["entity_id"])): row for row in snapshot.equipment}
        inventory_rows = {
            (UUID(str(row["owner_id"])), UUID(str(row["item_id"]))): row
            for row in snapshot.inventory
        }
        quantities = {key: _stored_int(row["quantity"]) for key, row in inventory_rows.items()}

        def resolve_ref(ref: str) -> UUID:
            try:
                return refs.resolve(ref)
            except ValueError as error:
                raise ResolutionInputError(str(error)) from error

        # Validate the entire plan against the initial snapshot before any Engine call.
        attackable = self._attackable_entity_ids(work, snapshot)
        skill_data: dict[str, tuple[Mapping[str, object], int]] = {}
        for raw_intent in intents:
            if isinstance(raw_intent, ScenarioActionIntent):
                continue
            if isinstance(raw_intent, OpenActionIntent):
                if snapshot.scenario_run is None or self._scenario_progressor is None:
                    raise ResolutionInputError("Open action requires a scenario")
                try:
                    self._scenario_progressor.progress_open(snapshot.scenario_run, raw_intent,
                                                           "success")
                    if raw_intent.check is not None:
                        self._scenario_progressor.progress_open(snapshot.scenario_run, raw_intent,
                                                               "failure")
                except ScenarioActionUnavailableError as error:
                    raise ResolutionInputError(str(error)) from error
                if raw_intent.check is not None:
                    saved = next((row for row in snapshot.abilities
                                  if row["character_id"] == work.actor_id), None)
                    if saved is None:
                        raise ResolutionInputError("Saved character abilities are missing")
                    try:
                        MvpV2Ruleset(self._ruleset.dice).open_modifier(
                            ability=raw_intent.check.ability,
                            skill_ref=raw_intent.check.skill_ref,
                            scores={key: _stored_int(saved[key]) for key in
                                    ("strength", "agility", "insight", "presence")},
                            specialty=str(saved["specialty_skill"]),
                        )
                    except ValueError as error:
                        raise ResolutionInputError(str(error)) from error
                continue
            if isinstance(raw_intent, SkillCheckIntent):
                if raw_intent.target_ref is not None:
                    raise ResolutionInputError("対象付き技能判定はMVPでは扱いません")
                checks = [
                    row
                    for row in snapshot.skill_checks
                    if row["scene_id"] == work.scene_id
                    and row["skill_ref"] == raw_intent.skill_ref
                    and row["target_id"] is None
                ]
                if len(checks) != 1:
                    raise ResolutionInputError("登録済みScene技能判定を一意に解決できません")
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
                skill_data[raw_intent.skill_ref] = (
                    checks[0],
                    _stored_int(modifier_row["modifier"]),
                )
            elif isinstance(raw_intent, AttackIntent):
                target_id = resolve_ref(raw_intent.target_ref)
                if target_id not in attackable:
                    raise ResolutionInputError("有効な攻撃対象ではありません")
                if characters[target_id].current_hp == 0:
                    raise ResolutionInputError("0 HPの対象は攻撃できません")
                if raw_intent.weapon_ref is not None:
                    equipped_id = resolve_ref(raw_intent.weapon_ref)
                    inventory = inventory_rows.get((work.actor_id, equipped_id))
                    if (
                        inventory is None
                        or equipped_id not in weapons
                        or not bool(inventory["equipped"])
                        or _stored_int(inventory["quantity"]) < 1
                    ):
                        raise ResolutionInputError("所有・装備したweaponではありません")
            elif isinstance(raw_intent, UseItemIntent):
                item_id = resolve_ref(raw_intent.item_ref)
                target_id = (
                    work.actor_id
                    if raw_intent.target_ref is None
                    else resolve_ref(raw_intent.target_ref)
                )
                if target_id != work.actor_id or raw_intent.item_ref != "healing_potion":
                    raise ResolutionInputError("登録済み回復itemの有効な対象ではありません")
                inventory = inventory_rows.get((work.actor_id, item_id))
                if inventory is None or _stored_int(inventory["quantity"]) < 1:
                    raise ResolutionInputError("actorが使用可能なitemを所有していません")
                if actor.current_hp >= actor.max_hp:
                    raise ResolutionInputError("HPが満タンのため回復itemを使用できません")
            else:
                raise ResolutionInputError("未対応のAction Intentです")

        return _ActionPreflight(
            scenario_bindings=scenario_bindings,
            characters=characters,
            refs=refs,
            id_to_ref=id_to_ref,
            weapons=weapons,
            quantities=quantities,
            skill_data=skill_data,
        )

    def _resolve_actions(
        self,
        work: ResolutionWorkItem,
        snapshot: CanonicalSnapshot,
        intents: Sequence[object],
        *,
        preflight: _ActionPreflight | None = None,
    ) -> tuple[
        tuple[ActionRecord, ...],
        list[ResolvedAction],
        list[ContextFragment],
        ScenarioProgressUpdate | None,
    ]:
        plan = preflight or self._preflight_actions(work, snapshot, intents)
        scenario_bindings = plan.scenario_bindings
        characters = plan.characters
        refs = plan.refs
        id_to_ref = plan.id_to_ref
        weapons = plan.weapons
        quantities = plan.quantities
        skill_data = plan.skill_data

        def resolve_ref(ref: str) -> UUID:
            try:
                return refs.resolve(ref)
            except ValueError as error:
                raise ResolutionInputError(str(error)) from error

        def character_with_hp(character: CharacterState, hp: int) -> CharacterState:
            return CharacterState(
                id=character.id,
                current_hp=hp,
                max_hp=character.max_hp,
                defense=character.defense,
                attack_bonus=character.attack_bonus,
            )

        records: list[ActionRecord] = []
        resolved: list[ResolvedAction] = []
        public_state: list[ContextFragment] = []
        scenario_update: ScenarioProgressUpdate | None = None
        draw_index = 0
        for ordinal, raw_intent in enumerate(intents, start=1):
            actor = characters[work.actor_id]
            command: (
                AttackCommand | SkillCheckCommand | UseItemCommand
                | ScenarioActionCommand | OpenActionCommand
            )
            result: AppliedResult | NotApplicableResult
            check: Mapping[str, object] | None = None
            binding = scenario_bindings[ordinal - 1]

            if isinstance(raw_intent, OpenActionIntent):
                saved = next((row for row in snapshot.abilities
                              if row["character_id"] == work.actor_id), None)
                if saved is None:
                    raise ResolutionInputError("Saved character abilities are missing")
                open_check = raw_intent.check
                modifier = None
                difficulty_class = None
                if open_check is not None:
                    try:
                        modifier = MvpV2Ruleset(self._ruleset.dice).open_modifier(
                            ability=open_check.ability, skill_ref=open_check.skill_ref,
                            scores={key: _stored_int(saved[key]) for key in
                                    ("strength", "agility", "insight", "presence")},
                            specialty=str(saved["specialty_skill"]),
                        )
                        difficulty_class = self._ruleset.difficulty_class(open_check.difficulty)
                    except ValueError as error:
                        raise ResolutionInputError(str(error)) from error
                command = OpenActionCommand(
                    kind="open_action", action_id=self._action_id_factory(),
                    campaign_id=work.campaign_id, turn_id=work.turn_id,
                    actor_id=work.actor_id, ordinal=ordinal,
                    approach=raw_intent.approach,
                    ability=None if open_check is None else open_check.ability,
                    skill_ref=None if open_check is None else open_check.skill_ref,
                    modifier=modifier, difficulty_class=difficulty_class,
                )
                if open_check is None:
                    result = AppliedResult(kind="applied", outcome="neutral",
                                           facts=[raw_intent.approach], dice=[], state_changes=[])
                else:
                    assert modifier is not None and difficulty_class is not None
                    roll = self._ruleset.dice.roll(f"1d20{modifier:+d}")
                    outcome = "success" if roll.total >= difficulty_class else "failure"
                    result = AppliedResult(
                        kind="applied", outcome=outcome,
                        facts=[f"{raw_intent.approach}: {outcome} (合計{roll.total})"],
                        dice=[roll], state_changes=[],
                    )
            elif isinstance(raw_intent, ScenarioActionIntent):
                assert isinstance(binding, DirectScenarioAction)
                command = ScenarioActionCommand(
                    kind="scenario_action",
                    action_id=self._action_id_factory(),
                    campaign_id=work.campaign_id,
                    turn_id=work.turn_id,
                    actor_id=work.actor_id,
                    ordinal=ordinal,
                    action_ref=raw_intent.action_ref,
                )
                result = AppliedResult(
                    kind="applied",
                    outcome="neutral",
                    facts=[binding.public_fact],
                    dice=[],
                    state_changes=[],
                )
            elif isinstance(raw_intent, SkillCheckIntent):
                check, modifier = skill_data[raw_intent.skill_ref]
                try:
                    difficulty_class = self._ruleset.difficulty_class(str(check["difficulty"]))
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
                    modifier=modifier,
                    difficulty_class=difficulty_class,
                )
                try:
                    result = self._ruleset.resolve_skill_check(command, actor)
                except ValueError as error:
                    raise ResolutionInputError(str(error)) from error
            elif isinstance(raw_intent, AttackIntent):
                target_id = resolve_ref(raw_intent.target_ref)
                target = characters[target_id]
                weapon_id: UUID | None = None
                damage_expression = "1d2"
                damage_bonus = 0
                if raw_intent.weapon_ref is not None:
                    weapon_id = resolve_ref(raw_intent.weapon_ref)
                    weapon = weapons[weapon_id]
                    damage_expression = str(weapon["damage_expression"])
                    damage_bonus = _stored_int(weapon["damage_bonus"])
                command = AttackCommand(
                    kind="attack",
                    action_id=self._action_id_factory(),
                    campaign_id=work.campaign_id,
                    turn_id=work.turn_id,
                    actor_id=work.actor_id,
                    ordinal=ordinal,
                    target_id=target_id,
                    weapon_id=weapon_id,
                    attack_bonus=actor.attack_bonus,
                    damage_expression=damage_expression,
                    damage_bonus=damage_bonus,
                )
                if target.current_hp == 0:
                    result = NotApplicableResult(kind="not_applicable", reason="target_unavailable")
                else:
                    try:
                        result = self._ruleset.resolve_attack(command, actor, target)
                    except ValueError as error:
                        raise ResolutionInputError(str(error)) from error
            elif isinstance(raw_intent, UseItemIntent):
                item_id = resolve_ref(raw_intent.item_ref)
                target_id = (
                    work.actor_id
                    if raw_intent.target_ref is None
                    else resolve_ref(raw_intent.target_ref)
                )
                command = UseItemCommand(
                    kind="use_item",
                    action_id=self._action_id_factory(),
                    campaign_id=work.campaign_id,
                    turn_id=work.turn_id,
                    actor_id=work.actor_id,
                    ordinal=ordinal,
                    item_id=item_id,
                    target_id=target_id,
                    effect_ref="healing_potion",
                )
                quantity = quantities[(work.actor_id, item_id)]
                if quantity == 0:
                    result = NotApplicableResult(
                        kind="not_applicable", reason="resource_unavailable"
                    )
                elif actor.current_hp >= actor.max_hp:
                    result = NotApplicableResult(kind="not_applicable", reason="rule_precondition")
                else:
                    try:
                        result = self._ruleset.resolve_use_item(
                            command, actor, actor, quantity=quantity
                        )
                    except ValueError as error:
                        raise ResolutionInputError(str(error)) from error
            else:
                raise ResolutionInputError("未対応のAction Intentです")

            dice = result.dice if isinstance(result, AppliedResult) else []
            rng = tuple(
                RNGMetadata(
                    source=self._rng_source,
                    implementation_version=self._rng_implementation_version,
                    draw_index=draw_index + offset,
                )
                for offset, _roll in enumerate(dice)
            )
            draw_index += len(dice)
            records.append(ActionRecord(command=command, result=result, rng=rng))
            resolved.append(
                ResolvedAction(
                    action_id=command.action_id,
                    ordinal=command.ordinal,
                    result=result,
                )
            )
            if isinstance(result, AppliedResult):
                for change in result.state_changes:
                    if isinstance(change, (DamageApplied, HealingApplied)):
                        current = characters.get(change.target_id)
                        if current is None or current.current_hp != change.hp_before:
                            raise ResolutionInputError("EngineのHP遷移が作業状態と一致しません")
                        characters[change.target_id] = character_with_hp(current, change.hp_after)
                        ref = id_to_ref.get(change.target_id)
                        if ref is not None:
                            public_state.append(
                                ContextFragment(
                                    source=f"entity:{ref}",
                                    trust_level="derived",
                                    access_scope="public",
                                    content=(f"@{ref} HP {change.hp_after}/{current.max_hp}"),
                                )
                            )
                    elif isinstance(change, ItemConsumed):
                        key = (change.owner_id, change.item_id)
                        if quantities.get(key) != change.quantity_before:
                            raise ResolutionInputError("Engineの在庫遷移が作業状態と一致しません")
                        quantities[key] = change.quantity_after
                        ref = id_to_ref.get(change.item_id)
                        if ref is not None:
                            public_state.append(
                                ContextFragment(
                                    source=f"inventory:{ref}",
                                    trust_level="derived",
                                    access_scope="actor_private",
                                    content=f"@{ref} 残数 {change.quantity_after}",
                                )
                            )
            if (
                isinstance(result, AppliedResult)
                and result.outcome == "success"
                and isinstance(raw_intent, SkillCheckIntent)
            ):
                assert isinstance(check, dict)
                public_state.append(
                    ContextFragment(
                        source=f"scene_check:{check['check_ref']}",
                        trust_level="trusted",
                        access_scope="public",
                        content=str(check["public_description"]),
                    )
                )
            if (
                isinstance(raw_intent, OpenActionIntent)
                and isinstance(result, AppliedResult)
                and snapshot.scenario_run is not None
                and self._scenario_progressor is not None
            ):
                scenario_update = self._scenario_progressor.progress_open(
                    snapshot.scenario_run, raw_intent,
                    "failure" if result.outcome == "failure" else "success",
                )
                public_state.append(self._scenario_public_state_after(
                    snapshot.scenario_run, scenario_update))
                public_state.extend(ContextFragment(
                    source="scenario_fact", trust_level="derived", access_scope="public",
                    content=fact.public_text,
                ) for fact in scenario_update.facts)
            if (
                binding is not None
                and isinstance(result, AppliedResult)
                and snapshot.scenario_run is not None
                and self._scenario_progressor is not None
            ):
                target_hp_after = (
                    characters[resolve_ref(raw_intent.target_ref)].current_hp
                    if isinstance(raw_intent, AttackIntent)
                    else None
                )
                scenario_update = self._scenario_progressor.progress_for(
                    snapshot.scenario_run,
                    binding,
                    result.outcome,
                    target_hp_after,
                )
                if scenario_update is not None:
                    public_state.append(
                        self._scenario_public_state_after(snapshot.scenario_run, scenario_update)
                    )
        if (
            scenario_update is None and snapshot.scenario_run is not None
            and snapshot.scenario_run.scenario_version >= 3
            and any(isinstance(record.result, AppliedResult) for record in records)
        ):
            scenario_update = ScenarioProgressUpdate(
                from_scene_id=work.scene_id, to_scene_id=None,
                add_flags=(), ending_ref=None, elapsed_actions=1,
            )
        return tuple(records), resolved, public_state, scenario_update

    def _scenario_bindings(
        self,
        snapshot: CanonicalSnapshot,
        intents: Sequence[object],
    ) -> list[ScenarioActionBinding | None]:
        run = snapshot.scenario_run
        if run is not None and run.scenario_version >= 3 and len(intents) != 1:
            raise ResolutionInputError("A v3 Turn must contain one action")
        if any(isinstance(intent, OpenActionIntent) for intent in intents) and len(intents) != 1:
            raise ResolutionInputError("Open action must be the only action in this Turn")
        if run is None:
            if any(isinstance(intent, ScenarioActionIntent) for intent in intents):
                raise ResolutionInputError("Scenario runのない直接行動です")
            return [None] * len(intents)
        if self._scenario_progressor is None:
            raise ResolutionInputError("Scenario進行componentが設定されていません")

        bindings: list[ScenarioActionBinding | None] = []
        for intent in intents:
            try:
                binding: ScenarioActionBinding | None
                if isinstance(intent, ScenarioActionIntent):
                    binding = self._scenario_progressor.bind_scenario_action(run, intent.action_ref)
                    if binding is None:
                        raise ResolutionInputError("登録済みScenario行動ではありません")
                elif isinstance(intent, SkillCheckIntent):
                    binding = self._scenario_progressor.bind_skill_check(run, intent.skill_ref)
                elif isinstance(intent, AttackIntent):
                    binding = self._scenario_progressor.bind_attack(run, intent.target_ref)
                else:
                    binding = None
            except ScenarioActionUnavailableError as error:
                raise ResolutionInputError(str(error)) from error
            bindings.append(binding)
        if sum(binding is not None for binding in bindings) >= 2:
            raise ResolutionInputError("一つのplanに複数のScenario進行行動があります")
        return bindings

    def _scenario_public_state_after(
        self,
        run: ScenarioRunSnapshot,
        update: ScenarioProgressUpdate,
    ) -> ContextFragment:
        assert self._scenario_progressor is not None
        definition = self._scenario_progressor.definition_for(run)
        if update.to_scene_id is not None:
            runtime_scene = next(scene for scene in run.scenes if scene.id == update.to_scene_id)
            scene = next(
                scene for scene in definition.scenes if scene.sequence == runtime_scene.sequence
            )
            source = "scenario_scene"
            parts = [f"{scene.title}: {scene.description}"]
        elif update.ending_ref is not None:
            ending = next(
                ending for ending in definition.endings if ending.ending_ref == update.ending_ref
            )
            source = "scenario_ending"
            parts = [f"{ending.title}: {ending.summary}"]
        else:
            source = "scenario_facts"
            parts = []
        parts.extend(
            flag.public_fact for flag in definition.flags if flag.flag_ref in update.add_flags
        )
        return ContextFragment(
            source=source,
            trust_level="trusted",
            access_scope="public",
            content=" / ".join(parts) or "現在の場面で行動を終えた。",
        )


class NarrationWorker:
    """保存済み公開snapshotだけをFakeまたは実transportへ渡して描写を確定する。"""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        narrator: ResultNarrator,
        policy: WorkerPhasePolicy,
        *,
        choice_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._narrator = narrator
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
            work = await unit_of_work.narration.get_work(lease.turn_id, lease.worker_epoch)
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

        try:
            async with asyncio.timeout(self._policy.request_timeout_seconds):
                if not await _reserve_call(
                    self._unit_of_work_factory, work.turn_id, "narration", work.worker_epoch
                ):
                    raise CallBudgetExceeded("LLM呼び出し予算を使い切りました")
                draft = await self._narrator.narrate_result(
                    work.narration_input, model_id=self._policy.model_id
                )
                validate_mechanical_narration(work.narration_input, draft)
        except TimeoutError:
            return await self._handle_failure(work, "MODEL_TIMEOUT")
        except ProviderRefusalError:
            return await self._handle_failure(work, "MODEL_REFUSAL")
        except (ValidationError, ProviderOutputError, NarrationGroundingError):
            return await self._handle_failure(work, "INVALID_OUTPUT")
        except CallBudgetExceeded:
            return await self._handle_failure(work, "UNKNOWN")
        except (ConnectionError, OSError):
            return await self._handle_failure(work, "UNKNOWN")
        except ProviderHTTPError:
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

    async def _handle_failure(self, narration_work: NarrationWorkItem, failure_code: str) -> bool:
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
