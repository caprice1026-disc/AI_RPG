"""Pure compilation and the authoring use cases shared by manual and AI editing."""

import hashlib
import json
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError

from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.contracts.stories import (
    AuthoringDraft,
    CreateStoryPlaytestRequest,
    CreateStoryRequest,
    DuplicateStoryRequest,
    MyStoriesResponse,
    PublishStoryRequest,
    RestoreStoryRequest,
    SaveStoryDraftRequest,
    StoryDraftResponse,
    StoryMetadata,
    StoryOwnerSummary,
    StoryPlaytestVersionResponse,
    StoryPublicDetail,
    StoryRevisionsResponse,
    StorySettingsRequest,
    StoryTemplatesResponse,
    StoryValidationReport,
    TemplateDefinition,
    TemplateQuestion,
    ValidateStoryRequest,
    ValidationFinding,
)
from ai_rpg.scenarios.catalog import BUILTIN_SCENARIOS
from ai_rpg.scenarios.models import (
    AttackScenarioAction,
    DirectScenarioAction,
    ScenarioDefinition,
    ScenarioEffect,
    SkillScenarioAction,
)

if TYPE_CHECKING:
    from ai_rpg.infrastructure.postgres.stories import PostgresStoryStore

VALIDATOR_VERSION = "story-validator-1"
BUILTIN_KEYS = (
    ("ruined_chapel", 1),
    ("ruined_chapel", 2),
    ("ruined_chapel", 3),
    ("mist_lighthouse", 1),
)


@dataclass(frozen=True)
class TemplateRegistryEntry:
    source_ref: str
    source_version: int
    version: int
    description: str
    title: str | None = None
    sections: tuple[str, ...] = (
        "metadata", "world", "scenes", "initialization", "flags", "endings", "field_policies",
    )
    questions: tuple[TemplateQuestion, ...] = ()
    recommended_structure: tuple[str, ...] = ()


TEMPLATE_REGISTRY = {
    "ruined_chapel": TemplateRegistryEntry(
        source_ref="ruined_chapel", source_version=3, version=1,
        description="探索と対決を含む短編",
        questions=(
            TemplateQuestion(
                prompt="冒険者は何を達成したいですか?",
                hint="目的と、達成せずに撤退する選択肢を考えます。",
                target_section="world", field_path="/scenario/objective",
            ),
            TemplateQuestion(
                prompt="目的の達成を阻む相手は誰ですか?",
                hint="相手の動機と、対話・隠密・戦闘で取れる方法を考えます。",
                target_section="initialization", field_path="/scenario/initialization/characters",
            ),
        ),
        recommended_structure=("依頼と探索への導入", "手掛かりの探索と対決", "帰還または撤退"),
    ),
    "mist_lighthouse": TemplateRegistryEntry(
        source_ref="mist_lighthouse", source_version=1, version=1,
        description="探索と交渉で目的を達成する短編",
        questions=(
            TemplateQuestion(
                prompt="冒険者に何を持ち帰ってほしいですか?",
                hint="依頼品の所在と、持ち帰らずに解決する方法を考えます。",
                target_section="world", field_path="/scenario/objective",
            ),
            TemplateQuestion(
                prompt="目的地までにどのような経路を選べますか?",
                hint="複数の進み方と、引き返せる経路を用意します。",
                target_section="scenes", field_path="/scenario/scenes",
            ),
        ),
        recommended_structure=(
            "依頼と経路の選択", "探索と関係者との交渉", "持ち帰り・別の解決・撤退",
        ),
    ),
}
TEMPLATE_OBJECT_LABELS = {
    "ruined_chapel": {"relic_location": "銀の聖印"},
    "mist_lighthouse": {"journal_location": "航海日誌"},
}


class StoryError(Exception):
    def __init__(self, code: str, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def content_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def templates() -> StoryTemplatesResponse:
    values = []
    for template_id, entry in TEMPLATE_REGISTRY.items():
        definition = BUILTIN_SCENARIOS.get(entry.source_ref, entry.source_version)
        payload = deepcopy(definition.model_dump(mode="json"))
        payload["schema_version"] = 2
        # Historical JSON kept location facts as prose. Managed templates explicitly
        # materialize their objects for schema 2 without changing the legacy releases.
        if payload.get("world") is not None and payload.get("initialization") is not None:
            world = payload["world"]
            for fact in world["protected_facts"]:
                if fact["kind"] in {"location", "existence"} and fact["entity_ref"] is None:
                    entity_ref = "object_" + fact["fact_ref"]
                    fact["entity_ref"] = entity_ref
                    payload["initialization"]["items"].append(
                        {
                            "ref": entity_ref,
                            "label": TEMPLATE_OBJECT_LABELS[entry.source_ref][fact["fact_ref"]],
                        }
                    )
                    payload["initialization"]["placements"].append(
                        {
                            "entity_ref": entity_ref,
                            "scene_ref": fact["scene_ref"],
                            "visibility": fact["visibility"],
                        }
                    )
                    if fact["kind"] == "location" and fact["scene_ref"] == world["goal_scene_ref"]:
                        fact["acquired_flag_ref"] = world["goal_flag_ref"]
        capabilities = set(definition.required_capabilities)
        if definition.world is not None and definition.ruleset_ref == "mvp_v2":
            capabilities.add("open_actions")
        for scene in definition.scenes:
            for action in scene.actions:
                if isinstance(action, SkillScenarioAction):
                    capabilities.add("skill_checks")
                if isinstance(action, AttackScenarioAction):
                    capabilities.add("combat")
        if definition.initialization is not None and any(
            item.effect_ref is not None for item in definition.initialization.items
        ):
            capabilities.add("item_use")
        if payload.get("world") is not None and any(
            fact.get("entity_ref") is not None for fact in payload["world"]["protected_facts"]
        ):
            capabilities.add("protected_facts")
        payload["required_capabilities"] = sorted(capabilities)
        draft = AuthoringDraft(
            scenario=payload,
            metadata=StoryMetadata(title=definition.title, synopsis=definition.objective),
            field_policies={"/scenario/objective": "fixed", "/scenario/world": "fillable"},
        )
        values.append(
            TemplateDefinition(
                template_id=template_id,
                version=entry.version,
                title=entry.title if entry.title is not None else definition.title,
                description=entry.description,
                required_capabilities=sorted(capabilities),
                sections=list(entry.sections),
                questions=list(entry.questions),
                recommended_structure=list(entry.recommended_structure),
                initial_draft=draft,
            )
        )
    return StoryTemplatesResponse(templates=values)


def assign_missing_refs(draft: AuthoringDraft) -> AuthoringDraft:
    """Assign opaque stable refs once; never derive them from labels or array positions."""
    payload = draft.model_dump(mode="json")
    scenario = payload["scenario"]
    groups = [
        (scenario.get(name), field)
        for name, field in (
            ("scenes", "scene_ref"),
            ("flags", "flag_ref"),
            ("endings", "ending_ref"),
        )
    ]
    for scene in scenario.get("scenes", []) if isinstance(scenario.get("scenes"), list) else []:
        if isinstance(scene, dict):
            groups.append((scene.get("actions"), "action_ref"))
    initial = scenario.get("initialization")
    if isinstance(initial, dict):
        groups.extend((initial.get(name), "ref") for name in ("characters", "items"))
    world = scenario.get("world")
    if isinstance(world, dict):
        groups.append((world.get("protected_facts"), "fact_ref"))
    for rows, field in groups:
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and not row.get(field):
                row[field] = "ref_" + uuid4().hex
    return AuthoringDraft.model_validate(payload)


def compile_draft(
    draft: AuthoringDraft,
    story_id: UUID,
    revision: int,
    *,
    state_limit: int = 10000,
) -> tuple[ScenarioDefinition | None, StoryValidationReport]:
    errors: list[ValidationFinding] = []
    warnings: list[ValidationFinding] = []
    coverage: dict[str, JsonValue] = {
        "typed_definition": False,
        "registered_actions": "not_run",
        "freeform": "not_verified",
        "narrative_meaning": "not_verified",
    }
    definition = None
    compiled_hash = None
    try:
        definition = ScenarioDefinition.model_validate(draft.scenario)
    except ValidationError as error:
        for failure in error.errors(include_url=False, include_input=False):
            errors.append(
                ValidationFinding(
                    code="invalid_definition",
                    severity="error",
                    field_path="/scenario/" + "/".join(str(part) for part in failure["loc"]),
                    message=failure["msg"],
                    suggestion="Complete or correct this field.",
                )
            )
    if definition is not None:
        compiled_hash = content_hash(definition.model_dump(mode="json"))
        coverage["typed_definition"] = True
        if definition.schema_version != 2 or definition.initialization is None:
            errors.append(
                ValidationFinding(
                    code="explicit_initialization_required",
                    severity="error",
                    field_path="/scenario/initialization",
                    message="New authored stories require schema 2 and explicit initialization.",
                )
            )
        if (
            len(definition.scenes) > 50
            or len(definition.flags) > 64
            or any(len(scene.actions) > 100 for scene in definition.scenes)
        ):
            errors.append(
                ValidationFinding(
                    code="scenario_size_limit",
                    severity="error",
                    field_path="/scenario",
                    message="Limit: 50 scenes, 64 flags and 100 actions per scene.",
                )
            )
        else:
            _explore(definition, state_limit, errors, warnings, coverage)
        if definition.world is not None:
            warnings.append(
                ValidationFinding(
                    code="freeform_unverified",
                    severity="warning",
                    field_path="/scenario/world",
                    message="Freeform actions, pressure and generated facts remain unverified.",
                )
            )
        warnings.append(
            ValidationFinding(
                code="narrative_review_required",
                severity="warning",
                field_path="/scenario",
                message="Meaning, secrets and quality need the author's review and playthrough.",
            )
        )
    return definition, StoryValidationReport(
        id=uuid4(),
        story_id=story_id,
        draft_revision=revision,
        content_hash=compiled_hash,
        draft_hash=content_hash(draft.model_dump(mode="json")),
        validator_version=VALIDATOR_VERSION,
        errors=errors,
        warnings=warnings,
        coverage=coverage,
        created_at=datetime.now(UTC),
    )


def _explore(
    definition: ScenarioDefinition,
    limit: int,
    errors: list[ValidationFinding],
    warnings: list[ValidationFinding],
    coverage: dict[str, JsonValue],
) -> None:
    scenes = {scene.scene_ref: scene for scene in definition.scenes}
    endings = {ending.ending_ref: ending for ending in definition.endings}
    start = (
        next(scene.scene_ref for scene in definition.scenes if scene.sequence == 1),
        frozenset[str](),
    )
    queue = deque([start])
    seen = {start}
    reverse: dict[tuple[str, frozenset[str]], set[tuple[str, frozenset[str]]]] = {}
    can_end: set[tuple[str, frozenset[str]]] = set()
    reached_endings: set[str] = set()
    reached_scenes: set[str] = set()
    exhausted = False
    combat = any(scene.combat is not None for scene in definition.scenes)
    if combat:
        warnings.append(
            ValidationFinding(
                code="combat_unverified",
                severity="warning",
                field_path="/scenario/initialization",
                message="Combat is abstract here; survival and balance need an author playthrough.",
            )
        )
    while queue and not exhausted:
        state = queue.popleft()
        scene_ref, flags = state
        scene = scenes[scene_ref]
        reached_scenes.add(scene_ref)
        for action in scene.actions:
            if not set(action.required_flags) <= flags or set(action.disabled_flags) & flags:
                continue
            effects: tuple[ScenarioEffect, ...]
            if isinstance(action, DirectScenarioAction):
                effects = (action.success,)
            elif isinstance(action, SkillScenarioAction):
                effects = (action.success, action.failure)
            else:
                effects = (action.defeated,)
            for effect in effects:
                resulting_flags = flags | frozenset(effect.add_flags)
                if isinstance(action, AttackScenarioAction) and scene.combat is not None:
                    resulting_flags |= {scene.combat.started_flag}
                if definition.world is not None:
                    world = definition.world
                    if (
                        world.goal_flag_ref in effect.add_flags
                        and scene_ref != world.goal_scene_ref
                    ):
                        continue
                    if any(
                        fact.acquired_flag_ref in effect.add_flags
                        and fact.acquired_flag_ref not in flags
                        and fact.scene_ref != scene_ref
                        for fact in world.protected_facts
                    ):
                        continue
                ending_ref = effect.ending_ref
                for override in effect.overrides:
                    if set(override.requires_flags) <= flags:
                        ending_ref = override.ending_ref
                        break
                if ending_ref is not None:
                    if set(endings[ending_ref].required_flags) <= resulting_flags:
                        reached_endings.add(ending_ref)
                        can_end.add(state)
                    continue
                target = (effect.next_scene_ref or scene_ref, resulting_flags)
                reverse.setdefault(target, set()).add(state)
                if target not in seen:
                    if len(seen) >= limit:
                        exhausted = True
                        break
                    seen.add(target)
                    queue.append(target)
            if exhausted:
                break
    coverage.update(
        {
            "registered_actions": "partial" if exhausted else "finite_flags",
            "states_explored": len(seen),
            "state_limit": limit,
            "reachable_scenes": [str(value) for value in sorted(reached_scenes)],
            "reachable_endings": [str(value) for value in sorted(reached_endings)],
            "combat": "abstract_outcomes" if combat else "not_applicable",
        }
    )
    if exhausted:
        warnings.append(
            ValidationFinding(
                code="exploration_limit",
                severity="warning",
                field_path="/scenario",
                message="State budget exhausted; unvisited paths remain unverified.",
            )
        )
        return
    winning = set(can_end)
    pending = list(can_end)
    while pending:
        for previous in reverse.get(pending.pop(), set()):
            if previous not in winning:
                winning.add(previous)
                pending.append(previous)
    if not reached_endings or seen - winning:
        # Open actions and HP can alter these paths; the abstraction cannot prove a hard lock.
        uncertain = definition.world is not None or combat
        finding = ValidationFinding(
            code="registered_path_dead_end" if uncertain else "unreachable_ending",
            severity="warning" if uncertain else "error",
            field_path="/scenario/scenes",
            message="Some reachable flag states have no registered route to an ending.",
            suggestion="Add a return/ending action or verify the unmodeled routes in playtest.",
        )
        (warnings if uncertain else errors).append(finding)
    if set(scenes) - reached_scenes or set(endings) - reached_endings:
        warnings.append(
            ValidationFinding(
                code="unreached_content",
                severity="warning",
                field_path="/scenario",
                message="Some scenes or endings were not reached through registered actions.",
            )
        )


class StoryService:
    def __init__(self, store: "PostgresStoryStore") -> None:
        self.store = store

    def templates(self) -> StoryTemplatesResponse:
        return templates()

    async def create(
        self, principal: AuthenticatedPrincipal, request: CreateStoryRequest
    ) -> StoryDraftResponse:
        draft = request.draft or AuthoringDraft()
        template_version = None
        if request.template_id is not None:
            template = next(
                (item for item in templates().templates if item.template_id == request.template_id),
                None,
            )
            if template is None:
                raise StoryError("template_not_found", 404)
            draft = template.initial_draft
            template_version = template.version
        return await self.store.create(
            principal.principal_id, request, draft, template_version=template_version,
        )

    async def get_draft(
        self, principal: AuthenticatedPrincipal, story_id: UUID
    ) -> StoryDraftResponse:
        return await self.store.get_draft(principal.principal_id, story_id)

    async def list_owned(self, principal: AuthenticatedPrincipal) -> MyStoriesResponse:
        return await self.store.list_owned(principal.principal_id)

    async def save(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: SaveStoryDraftRequest
    ) -> StoryDraftResponse:
        return await self.store.save(principal.principal_id, story_id, request)

    async def revisions(
        self, principal: AuthenticatedPrincipal, story_id: UUID
    ) -> StoryRevisionsResponse:
        return await self.store.revisions(principal.principal_id, story_id)

    async def restore(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: RestoreStoryRequest
    ) -> StoryDraftResponse:
        return await self.store.restore(principal.principal_id, story_id, request)

    async def duplicate(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: DuplicateStoryRequest
    ) -> StoryDraftResponse:
        return await self.store.duplicate(principal.principal_id, story_id, request)

    async def validate(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: ValidateStoryRequest
    ) -> StoryValidationReport:
        cached = await self.store.cached_validation(
            principal.principal_id, story_id, request.expected_revision,
        )
        if cached is not None:
            return cached
        draft = await self.get_draft(principal, story_id)
        if draft.revision != request.expected_revision:
            raise StoryError("revision_conflict")
        _, report = compile_draft(draft.draft, story_id, draft.revision)
        await self.store.save_validation(principal.principal_id, report)
        return report

    async def create_playtest_version(
        self,
        principal: AuthenticatedPrincipal,
        story_id: UUID,
        request: CreateStoryPlaytestRequest,
    ) -> StoryPlaytestVersionResponse:
        previous = await self.store.replay(
            principal.principal_id,
            story_id,
            request,
            "playtest",
        )
        if previous is not None:
            return StoryPlaytestVersionResponse.model_validate(previous)
        report = await self.validate(
            principal, story_id, ValidateStoryRequest(expected_revision=request.expected_revision),
        )
        if report.errors:
            raise StoryError("validation_failed", 422)
        draft = await self.get_draft(principal, story_id)
        if draft.revision != request.expected_revision:
            raise StoryError("revision_conflict")
        # Reuse the admitted traversal; deserialization does not explore the graph again.
        definition = ScenarioDefinition.model_validate(draft.draft.scenario)
        return await self.store.create_playtest_version(
            principal.principal_id,
            story_id,
            request,
            definition,
            report,
        )

    async def publish(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: PublishStoryRequest
    ) -> StoryPublicDetail:
        return await self.store.publish(principal.principal_id, story_id, request)

    async def detail(self, principal: AuthenticatedPrincipal, story_id: UUID) -> StoryPublicDetail:
        return await self.store.detail(principal.principal_id, story_id)

    async def settings(
        self, principal: AuthenticatedPrincipal, story_id: UUID, request: StorySettingsRequest
    ) -> StoryOwnerSummary:
        return await self.store.settings(principal.principal_id, story_id, request)
