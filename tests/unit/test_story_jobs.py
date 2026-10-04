"""Adversarial proposal and owner-only router contracts."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_rpg.api.story_jobs import create_story_jobs_router
from ai_rpg.application.auth import AuthenticatedPrincipal
from ai_rpg.application.ports.story_authoring import AuthoringInput
from ai_rpg.application.stories import StoryError, templates
from ai_rpg.application.story_jobs import (
    AuthoringLimits,
    adopt_changes,
    prepare_proposal,
)
from ai_rpg.config import Settings
from ai_rpg.contracts.stories import AuthoringDraft
from ai_rpg.contracts.story_jobs import (
    CreateAuthoringJobRequest,
    GeneratedStoryChange,
    GeneratedStoryOutput,
)
from ai_rpg.infrastructure.postgres.story_jobs import PostgresStoryJobStore
from ai_rpg.llm.story_authoring import authoring_prompt


def change(path: str, value: object) -> GeneratedStoryChange:
    return GeneratedStoryChange(field_path=path, value_json=json.dumps(value), reason="Suggestion")


def test_fixed_descendants_positional_policies_and_stable_refs() -> None:
    draft = AuthoringDraft(
        scenario={
            "scenes": [
                {"scene_ref": "a", "title": "fixed secret"},
                {"scene_ref": "b", "title": "old"},
            ]
        },
        field_policies={"/scenario/scenes/0/title": "fixed"},
    )
    output = GeneratedStoryOutput(
        changes=[
            change(
                "/scenario/scenes",
                [
                    {"scene_ref": "b", "title": "better"},
                    {"scene_ref": "a", "title": "attacker"},
                ],
            ),
            change("/field_policies", {}),
            change("/scenario/scenario_ref", "hijack"),
        ]
    )
    proposal = prepare_proposal(uuid4(), uuid4(), 10, draft, output)
    assert len(proposal.changes) == 1
    assert proposal.changes[0].field_path == "/scenario/scenes/@scene_ref=b/title"
    assert len(proposal.findings) == 3
    adopted = adopt_changes(draft, proposal, [proposal.changes[0].id])
    assert adopted.scenario["scenes"] == [
        {"scene_ref": "a", "title": "fixed secret"},
        {"scene_ref": "b", "title": "better"},
    ]
    assert adopted.field_policies == draft.field_policies


@pytest.mark.parametrize(
    "path,value",
    [
        ("/scenario/world", {"secret": "hacked"}),
        ("/scenario/world", None),
        ("/scenario/world/secret", "hacked"),
    ],
)
def test_fixed_container_or_descendant_cannot_be_replaced(path: str, value: object) -> None:
    draft = AuthoringDraft(
        scenario={"world": {"secret": "kept"}}, field_policies={"/scenario/world/secret": "fixed"}
    )
    proposal = prepare_proposal(
        uuid4(), uuid4(), 1, draft, GeneratedStoryOutput(changes=[change(path, value)])
    )
    assert not proposal.changes
    assert proposal.findings


def test_undecided_is_explicit_proposal_not_a_policy_change() -> None:
    draft = AuthoringDraft(field_policies={"/metadata/title": "undecided"})
    proposal = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(
            changes=[
                change("/metadata/title", "Possible title"),
                change("/metadata/synopsis", "Optional synopsis"),
            ]
        ),
    )
    assert proposal.changes[0].policy == "undecided"
    adopted = adopt_changes(draft, proposal, [proposal.changes[0].id])
    assert adopted.metadata.title == "Possible title"
    assert adopted.metadata.synopsis == ""
    assert adopted.field_policies["/metadata/title"] == "undecided"
    assert draft.metadata.title == ""


def test_check_never_proposes_mutations_and_compile_reports_invalid_mechanics() -> None:
    draft = AuthoringDraft()
    checked = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(changes=[change("/metadata/title", "Changed")]),
        check_only=True,
    )
    assert not checked.changes
    invalid = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(
            changes=[change("/scenario/required_capabilities", ["arbitrary_code"])]
        ),
    )
    assert invalid.validation.errors
    assert invalid.decision == "pending"


@pytest.mark.parametrize("path", ["/scenario/scenes/0/title", "/scenario/~2escape", "/scenario//x"])
def test_ambiguous_or_numeric_patch_paths_are_rejected(path: str) -> None:
    with pytest.raises(StoryError, match="invalid_proposal_path"):
        prepare_proposal(
            uuid4(),
            uuid4(),
            1,
            AuthoringDraft(scenario={"scenes": [{"scene_ref": "a", "title": "keep"}]}),
            GeneratedStoryOutput(changes=[change(path, "evil")]),
        )


def test_model_and_runtime_injection_cannot_be_supplied_as_request_fields() -> None:
    for injected in (
        {"model_id": "attacker:proxy"},
        {"provider_url": "https://evil"},
        {"max_total_tokens": 99999999},
        {"owner_principal_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            CreateAuthoringJobRequest.model_validate(
                {"request_id": str(uuid4()), "base_revision": 1, "kind": "fill", **injected}
            )
    with pytest.raises(ValidationError):
        CreateAuthoringJobRequest(request_id=uuid4(), base_revision=1, kind="concretize")


def test_policy_alias_cannot_override_fixed_and_unknown_ids_do_not_apply() -> None:
    draft = AuthoringDraft(
        scenario={"scenes": [{"scene_ref": "a", "title": "kept"}]},
        field_policies={
            "/scenario/scenes/0/title": "fixed",
            "/scenario/scenes/@scene_ref=a/title": "fillable",
        },
    )
    proposal = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(changes=[change("/scenario/scenes/@scene_ref=a/title", "evil")]),
    )
    assert not proposal.changes
    with pytest.raises(StoryError, match="unknown_proposal_change"):
        adopt_changes(draft, proposal, [uuid4()])


def test_identity_refs_are_immutable_but_branch_and_location_references_are_editable() -> None:
    draft = AuthoringDraft(
        scenario={
            "scenes": [
                {
                    "scene_ref": "a",
                    "actions": [
                        {"action_ref": "act", "success": {"ending_ref": "old"}},
                    ],
                }
            ],
            "world": {
                "protected_facts": [
                    {"fact_ref": "first", "scene_ref": "a", "statement": "fixed"},
                    {"fact_ref": "second", "scene_ref": "a", "statement": "old"},
                ]
            },
        },
        field_policies={"/scenario/world/protected_facts/0/statement": "fixed"},
    )
    proposal = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(
            changes=[
                change(
                    "/scenario/scenes/@scene_ref=a/actions/@action_ref=act/success/ending_ref",
                    "new",
                ),
                change("/scenario/scenes/@scene_ref=a/scene_ref", "renamed"),
                change(
                    "/scenario/world/protected_facts",
                    [
                        {"fact_ref": "first", "scene_ref": "a", "statement": "evil"},
                        {"fact_ref": "second", "scene_ref": "a", "statement": "new"},
                    ],
                ),
            ]
        ),
    )
    assert len(proposal.changes) == 2
    assert len(proposal.findings) == 2
    assert proposal.changes[1].field_path.endswith("/@fact_ref=second/statement")
    adopted = adopt_changes(draft, proposal, [c.id for c in proposal.changes])
    assert adopted.scenario["scenes"][0]["actions"][0]["success"]["ending_ref"] == "new"


def test_concretize_blank_draft_uses_supported_server_schema_and_common_compiler() -> None:
    template = templates().templates[1].initial_draft
    draft = AuthoringDraft(scenario={"scenario_ref": "example", "version": 1})
    changes = [
        change("/scenario/" + key, value)
        for key, value in template.scenario.items()
        if key not in {"schema_version", "scenario_ref", "version"}
    ]
    proposal = prepare_proposal(
        uuid4(), uuid4(), 1, draft, GeneratedStoryOutput(changes=changes), concretize=True
    )
    assert not proposal.validation.errors
    adopted = adopt_changes(draft, proposal, [c.id for c in proposal.changes])
    assert adopted.scenario["schema_version"] == 2
    assert adopted.scenario["scenario_ref"] == "example"
    # A fixed empty scenario remains unchanged even during schema initialization.
    fixed = draft.model_copy(update={"field_policies": {"/scenario": "fixed"}})
    rejected = prepare_proposal(
        uuid4(), uuid4(), 1, fixed, GeneratedStoryOutput(changes=changes), concretize=True
    )
    assert not rejected.changes


def test_whole_scenario_candidate_is_diffed_without_overwriting_identity_or_fixed_fields() -> None:
    template = templates().templates[1].initial_draft
    draft = AuthoringDraft(
        scenario={"scenario_ref": "mine", "version": 1, "objective": "Keep it"},
        field_policies={"/scenario/objective": "fixed"},
    )
    proposal = prepare_proposal(
        uuid4(),
        uuid4(),
        1,
        draft,
        GeneratedStoryOutput(changes=[change("/scenario", template.scenario)]),
        concretize=True,
    )
    assert proposal.changes and all(c.field_path != "/scenario" for c in proposal.changes)
    adopted = adopt_changes(draft, proposal, [c.id for c in proposal.changes])
    assert adopted.scenario["scenario_ref"] == "mine"
    assert adopted.scenario["objective"] == "Keep it"
    assert adopted.field_policies == draft.field_policies
    assert not proposal.validation.errors


def test_settings_and_template_prompt_byte_budget_include_schema() -> None:
    settings = Settings(authoring_max_prompt_bytes=128000, concurrent_authoring_job_limit=3)
    limits = AuthoringLimits.from_settings(settings)
    assert limits.max_active_per_owner == 3
    assert limits.timeout_seconds == settings.authoring_timeout_seconds
    for template in templates().templates:
        prompt = authoring_prompt(AuthoringInput("fill", template.initial_draft, "", None))
        assert "scenario_schema" in prompt
        assert 14000 < len(prompt.encode()) < limits.max_prompt_bytes


def test_router_all_private_operations_auth_and_sanitized_conflicts() -> None:
    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="owner",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset(),
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    store = MagicMock(spec=PostgresStoryJobStore)
    store.get = AsyncMock(side_effect=StoryError("authoring_job_not_found", 404))
    store.apply = AsyncMock(side_effect=StoryError("proposal_base_conflict"))
    app = FastAPI()
    app.include_router(create_story_jobs_router(principal_provider=authenticated, store=store))
    with TestClient(app) as client:
        assert client.get(f"/authoring-jobs/{uuid4()}").json() == {
            "detail": {"code": "authoring_job_not_found"}
        }
        response = client.post(
            f"/stories/{uuid4()}/proposals/{uuid4()}/apply",
            json={
                "request_id": str(uuid4()),
                "expected_revision": 10,
                "change_ids": [str(uuid4())],
            },
        )
        assert response.status_code == 409
        assert store.apply.call_args.args[0] == principal.principal_id

    async def unauthenticated() -> AuthenticatedPrincipal:
        raise HTTPException(401)

    app.dependency_overrides[authenticated] = unauthenticated
    store.reset_mock()
    with TestClient(app) as client:
        for method, path in [
            ("GET", f"/authoring-jobs/{uuid4()}"),
            ("POST", f"/authoring-jobs/{uuid4()}/cancel"),
        ]:
            assert client.request(method, path).status_code == 401
    store.get.assert_not_called()
    store.cancel.assert_not_called()
