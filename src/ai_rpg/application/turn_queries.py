"""認可付きTurn公開DTO取得ユースケース。"""

from collections.abc import Callable
from uuid import UUID

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports import (
    AuthorizationError,
    AuthorizationPolicy,
    ScenarioRunSnapshot,
    UnitOfWork,
)
from ai_rpg.application.scenarios import ScenarioProgressor, ScenarioStateError
from ai_rpg.contracts import (
    AdventureAction,
    AdventureEnding,
    AdventureScene,
    AdventureState,
    CampaignStateResponse,
    TurnResponse,
)
from ai_rpg.scenarios import ScenarioCatalog


class TurnNotFoundError(Exception):
    """指定されたCampaign内に公開可能なTurnが存在しない。"""

    code = "TURN_NOT_FOUND"


class TurnQueryService:
    def __init__(
        self,
        authorization: AuthorizationPolicy,
        unit_of_work_factory: Callable[[], UnitOfWork],
        scenario_catalog: ScenarioCatalog,
    ) -> None:
        self._authorization = authorization
        self._unit_of_work_factory = unit_of_work_factory
        self._scenario_progressor = ScenarioProgressor(scenario_catalog)

    async def get(
        self,
        principal: AuthenticatedPrincipal,
        campaign_id: UUID,
        turn_id: UUID,
    ) -> TurnResponse:
        if not await self._authorization.can_access_campaign(
            principal.principal_id, campaign_id
        ):
            raise AuthorizationError("Campaignを参照する権限がありません")
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.get_response(campaign_id, turn_id)
            if response is None:
                raise TurnNotFoundError("Turnが存在しません")
            await unit_of_work.commit()
        return response

    async def get_campaign_state(
        self,
        principal: AuthenticatedPrincipal,
        campaign_id: UUID,
    ) -> CampaignStateResponse:
        if not await self._authorization.can_access_campaign(
            principal.principal_id, campaign_id
        ):
            raise AuthorizationError("Campaignを参照する権限がありません")
        async with self._unit_of_work_factory() as unit_of_work:
            response = await unit_of_work.turns.get_campaign_state(campaign_id)
            scenario = await unit_of_work.scenarios.snapshot(campaign_id)
            if scenario is not None:
                response = response.model_copy(
                    update={"adventure": self._adventure_state(scenario)}
                )
            await unit_of_work.commit()
        return response

    def _adventure_state(self, snapshot: ScenarioRunSnapshot) -> AdventureState:
        definition = self._scenario_progressor.definition_for(snapshot)
        ending = None
        current_scene = None
        available_actions: list[AdventureAction] = []

        if snapshot.status == "active":
            context = self._scenario_progressor.public_context_for(snapshot)
            active = next(scene for scene in snapshot.scenes if scene.status == "active")
            scene = next(
                scene for scene in definition.scenes if scene.sequence == active.sequence
            )
            current_scene = AdventureScene(
                scene_ref=scene.scene_ref,
                title=context.scene_title,
                description=context.scene_description,
            )
            discovered_facts = list(context.discovered_facts)
            available_actions = [
                AdventureAction(action_ref=action_ref, label=label)
                for action_ref, label in context.available_actions
            ]
            status = "active"
        elif snapshot.status == "completed":
            discovered_facts = [
                flag.public_fact
                for flag in definition.flags
                if flag.flag_ref in snapshot.flags
            ]
            ending_definition = next(
                (
                    candidate
                    for candidate in definition.endings
                    if candidate.ending_ref == snapshot.ending_ref
                ),
                None,
            )
            if ending_definition is None:
                raise ScenarioStateError("完了済みScenarioのEnding定義が一致しません")
            ending = AdventureEnding(
                ending_ref=ending_definition.ending_ref,
                title=ending_definition.title,
                summary=ending_definition.summary,
            )
            status = "completed"
        else:
            raise ScenarioStateError("Scenario runのstatusが不正です")

        return AdventureState(
            scenario_ref=definition.scenario_ref,
            title=definition.title,
            objective=definition.objective,
            status=status,
            current_scene=current_scene,
            discovered_facts=discovered_facts,
            available_actions=available_actions,
            ending=ending,
        )
