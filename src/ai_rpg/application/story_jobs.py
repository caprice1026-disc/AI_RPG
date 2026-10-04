"""Durable authoring worker and pure proposal validation.

Integration: instantiate PostgresStoryJobStore(sessions, model_id=settings.background_model),
mount create_story_jobs_router(principal_provider=..., store=store), and run
StoryAuthoringWorker(store, PydanticAIStoryAuthoring(configured_models)).run_once()
in a separate worker loop. Get models from llm.models.build_provider_models(settings).
Their clients must outlive the loop; the factory disables SDK retries.
This worker uses no agent tools, no raw HTTP, and never publishes or creates versions.
"""

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from ai_rpg.application.ports.story_authoring import (
    AuthoringGenerationError,
    AuthoringInput,
    AuthoringUsage,
    StoryAuthoringLLM,
)
from ai_rpg.application.stories import StoryError, compile_draft
from ai_rpg.config import Settings
from ai_rpg.contracts.stories import AuthoringDraft, ValidationFinding
from ai_rpg.contracts.story_jobs import (
    GeneratedStoryOutput,
    StoryProposal,
    StoryProposalChange,
)

if TYPE_CHECKING:
    from ai_rpg.infrastructure.postgres.story_jobs import PostgresStoryJobStore


@dataclass(frozen=True)
class AuthoringLimits:
    timeout_seconds: float = 90
    lease_seconds: int = 120
    max_attempts: int = 2  # Only before a physical request was reserved.
    max_prompt_bytes: int = 128000
    max_output_tokens: int = 8192
    max_total_tokens: int = 24000
    max_active_per_owner: int = 2
    max_running: int = 4

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuthoringLimits":
        return cls(
            timeout_seconds=settings.authoring_timeout_seconds,
            lease_seconds=settings.authoring_lease_seconds,
            max_prompt_bytes=settings.authoring_max_prompt_bytes,
            max_output_tokens=settings.authoring_max_output_tokens,
            max_total_tokens=settings.authoring_max_total_tokens,
            max_active_per_owner=settings.concurrent_authoring_job_limit,
            max_running=settings.authoring_max_running,
        )

    def __post_init__(self) -> None:
        if (
            self.timeout_seconds <= 0
            or self.lease_seconds <= self.timeout_seconds
            or min(
                self.max_attempts,
                self.max_prompt_bytes,
                self.max_output_tokens,
                self.max_total_tokens,
                self.max_active_per_owner,
                self.max_running,
            )
            < 1
            or self.max_output_tokens > self.max_total_tokens
        ):
            raise ValueError("Invalid authoring limits")


_REFS = ("scene_ref", "action_ref", "flag_ref", "ending_ref", "fact_ref", "ref")
_COLLECTION_REFS = {
    "scenes": "scene_ref",
    "actions": "action_ref",
    "flags": "flag_ref",
    "endings": "ending_ref",
    "protected_facts": "fact_ref",
    "characters": "ref",
    "items": "ref",
}
_MISSING = object()


def _parts(path: str) -> list[str]:
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise StoryError("invalid_proposal_path", 422)
    parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    if not all(parts):
        raise StoryError("invalid_proposal_path", 422)
    return parts


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _ref(row: Any, key: str | None) -> str | None:
    if not isinstance(row, dict) or key is None:
        return None
    if isinstance(row.get(key), str):
        value = row[key]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            raise StoryError("invalid_entity_ref", 422)
        return f"@{key}={value}"
    return None


def _key(node: Any, part: str, *, missing: bool = False) -> str | int:
    if isinstance(node, dict):
        return part
    if isinstance(node, list) and part.startswith("@"):
        ref_key = part[1:].partition("=")[0]
        if ref_key not in _REFS:
            raise StoryError("invalid_proposal_path", 422)
        found = [i for i, row in enumerate(node) if _ref(row, ref_key) == part]
        if len(found) == 1:
            return found[0]
        if not found and missing:
            return len(node)
    raise StoryError("invalid_proposal_path", 422)


def _read(payload: Any, path: str) -> Any:
    node = payload
    for part in _parts(path):
        try:
            node = node[_key(node, part)]
        except (KeyError, IndexError, TypeError, StoryError):
            return _MISSING
    return node


def _write(payload: Any, path: str, value: Any, *, remove: bool = False) -> None:
    parts = _parts(path)
    node = payload
    for part in parts[:-1]:
        try:
            node = node[_key(node, part)]
        except (KeyError, IndexError, TypeError):
            raise StoryError("invalid_proposal_path", 422) from None
    key = _key(node, parts[-1], missing=not remove)
    if remove:
        try:
            del node[key]
        except (KeyError, IndexError):
            raise StoryError("invalid_proposal_path", 422) from None
    elif isinstance(node, list):
        assert isinstance(key, int)
        if _ref(value, parts[-1][1:].partition("=")[0]) != parts[-1]:
            raise StoryError("entity_ref_is_immutable", 422)
        if key == len(node):
            node.append(deepcopy(value))
        else:
            node[key] = deepcopy(value)
    elif isinstance(node, dict):
        node[key] = deepcopy(value)
    else:
        raise StoryError("invalid_proposal_path", 422)


def _canonical_policy(payload: Any, path: str) -> str:
    """Bind legacy positional policies to the original entity, before any edits."""
    node = payload
    result: list[str] = []
    for part in _parts(path):
        if isinstance(node, list) and part.isdecimal():
            index = int(part)
            ref_key = _COLLECTION_REFS.get(result[-1])
            if index >= len(node) or _ref(node[index], ref_key) is None:
                # Non-entity arrays are atomic: conservatively protect their container.
                return "/" + "/".join(result)
            part = str(_ref(node[index], ref_key))
        result.append(_escape(part))
        try:
            node = node[_key(node, part)]
        except (KeyError, IndexError, TypeError, StoryError):
            node = None
    return "/" + "/".join(result)


def _overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _policies(draft: AuthoringDraft) -> dict[str, str]:
    payload = draft.model_dump(mode="json")
    policies: dict[str, str] = {}
    priority = {"fillable": 0, "undecided": 1, "fixed": 2}
    for path, policy in draft.field_policies.items():
        canonical = _canonical_policy(payload, path)
        if priority[policy] >= priority[policies.get(canonical, "fillable")]:
            policies[canonical] = policy
    return policies


def _allowed(path: str) -> bool:
    parts = _parts(path)
    if len(parts) < 2 or parts[0] not in {"metadata", "scenario"}:
        return False
    # Runtime identifiers, permissions and field policies are never model-writable.
    return not any(
        part in {"scenario_ref", "version", "schema_version"}
        or (part in _REFS and parts[index - 1].startswith("@" + part + "="))
        for index, part in enumerate(parts[1:], start=1)
    )


def _diff(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            change
            for key in sorted(before.keys() | after.keys())
            for change in _diff(
                before.get(key, _MISSING), after.get(key, _MISSING), path + "/" + _escape(key)
            )
        ]
    if isinstance(before, list) and isinstance(after, list):
        ref_key = _COLLECTION_REFS.get(path.rsplit("/", 1)[-1])
        old_refs = [_ref(row, ref_key) for row in before]
        new_refs = [_ref(row, ref_key) for row in after]
        if all(old_refs + new_refs) and (old_refs or new_refs):
            if len(set(old_refs)) != len(old_refs) or len(set(new_refs)) != len(new_refs):
                raise StoryError("duplicate_entity_ref", 422)
            old = {str(key): row for key, row in zip(old_refs, before, strict=True)}
            new = {str(key): row for key, row in zip(new_refs, after, strict=True)}
            return [
                change
                for key in sorted(old.keys() | new.keys())
                for change in _diff(
                    old.get(key, _MISSING), new.get(key, _MISSING), path + "/" + str(key)
                )
            ]
    return [(path, before, after)]


def prepare_proposal(
    job_id: UUID,
    story_id: UUID,
    revision: int,
    base: AuthoringDraft,
    output: GeneratedStoryOutput,
    *,
    check_only: bool = False,
    concretize: bool = False,
) -> StoryProposal:
    original = base.model_dump(mode="json")
    candidate = deepcopy(original)
    policies = _policies(base)
    findings = list(output.findings)
    changes = []
    # The supported compilation schema is supplied by the server, not the model.
    schema_path = "/scenario/schema_version"
    if (
        concretize
        and "schema_version" not in base.scenario
        and not any(
            policy == "fixed" and _overlap(path, schema_path) for path, policy in policies.items()
        )
    ):
        candidate["scenario"]["schema_version"] = 2
        changes.append(
            StoryProposalChange(
                id=uuid4(),
                field_path=schema_path,
                operation="set",
                before_exists=False,
                after=2,
                reason="Initialize the server-supported authored scenario schema.",
            )
        )
    for generated in output.changes:
        # Models may return a whole scenario candidate. Expand it into protected,
        # field-level diffs below; never persist/adopt that replacement verbatim.
        container_candidate = generated.field_path in {"/scenario", "/metadata"}
        if check_only or (not container_candidate and not _allowed(generated.field_path)):
            findings.append(
                ValidationFinding(
                    code="protected_field",
                    severity="warning",
                    field_path=generated.field_path,
                    message="This field cannot be changed by AI.",
                )
            )
            continue
        value = json.loads(generated.value_json)
        tentative = deepcopy(candidate)
        _write(tentative, generated.field_path, value, remove=generated.operation == "remove")
        for path, before, after in _diff(candidate, tentative):
            if not _allowed(path) or any(
                policy == "fixed" and _overlap(path, protected)
                for protected, policy in policies.items()
            ):
                findings.append(
                    ValidationFinding(
                        code="fixed_field_change",
                        severity="warning",
                        field_path=path,
                        message="AI change rejected; the author's fixed value is kept.",
                    )
                )
                continue
            # Ancestor replacements cannot strip protected children, including missing fields.
            trial = deepcopy(candidate)
            _write(trial, path, None if after is _MISSING else after, remove=after is _MISSING)
            if any(
                policy == "fixed" and _read(original, protected) != _read(trial, protected)
                for protected, policy in policies.items()
            ):
                findings.append(
                    ValidationFinding(
                        code="fixed_field_change",
                        severity="warning",
                        field_path=path,
                        message="AI change rejected; a fixed descendant is protected.",
                    )
                )
                continue
            candidate = trial
            changes.append(
                StoryProposalChange(
                    id=uuid4(),
                    field_path=path,
                    operation="remove" if after is _MISSING else "set",
                    before_exists=before is not _MISSING,
                    before=None if before is _MISSING else before,
                    after=None if after is _MISSING else after,
                    reason=generated.reason,
                    policy="undecided"
                    if any(p == "undecided" and _overlap(path, k) for k, p in policies.items())
                    else "fillable",
                )
            )
    # Overlapping changes make selective adoption order-dependent; require another proposal.
    if any(
        _overlap(a.field_path, b.field_path)
        for i, a in enumerate(changes)
        for b in changes[i + 1 :]
    ):
        raise StoryError("overlapping_proposal_changes", 422)
    draft = AuthoringDraft.model_validate(candidate)
    _, validation = compile_draft(draft, story_id, revision)
    return StoryProposal(
        id=uuid4(),
        job_id=job_id,
        story_id=story_id,
        base_revision=revision,
        changes=changes,
        findings=findings,
        validation=validation,
    )


def adopt_changes(base: AuthoringDraft, proposal: StoryProposal, ids: list[UUID]) -> AuthoringDraft:
    selected = set(ids)
    if not selected <= {change.id for change in proposal.changes}:
        raise StoryError("unknown_proposal_change", 422)
    payload = base.model_dump(mode="json")
    policies = _policies(base)
    for change in proposal.changes:
        if change.id not in selected:
            continue
        schema_initialization = (
            change.field_path == "/scenario/schema_version"
            and not change.before_exists
            and change.operation == "set"
            and change.after == 2
            and "schema_version" not in base.scenario
        )
        if (not _allowed(change.field_path) and not schema_initialization) or any(
            policy == "fixed" and _overlap(change.field_path, path)
            for path, policy in policies.items()
        ):
            raise StoryError("fixed_field_change", 422)
        before = _read(payload, change.field_path)
        if (before is not _MISSING) != change.before_exists or (
            change.before_exists and before != change.before
        ):
            raise StoryError("proposal_base_conflict")
        _write(payload, change.field_path, change.after, remove=change.operation == "remove")
    if any(
        policy == "fixed" and _read(base.model_dump(mode="json"), path) != _read(payload, path)
        for path, policy in policies.items()
    ):
        raise StoryError("fixed_field_change", 422)
    return AuthoringDraft.model_validate(payload)


class StoryAuthoringWorker:
    def __init__(self, store: "PostgresStoryJobStore", llm: StoryAuthoringLLM) -> None:
        self.store = store
        self.llm = llm

    async def run_once(self) -> bool:
        lease = await self.store.claim()
        if lease is None:
            return False
        usage = AuthoringUsage()
        try:
            limits = lease.limits
            prompt = self.llm.prepare(
                AuthoringInput(lease.kind, lease.snapshot, lease.instructions, lease.outline),
                model_id=lease.model_id,
                max_bytes=limits.max_prompt_bytes,
            )
            if not await self.store.reserve_call(lease):
                return True
            async with asyncio.timeout(limits.timeout_seconds):
                result = await self.llm.generate(
                    prompt,
                    model_id=lease.model_id,
                    usage=usage,
                    max_output_tokens=limits.max_output_tokens,
                    max_total_tokens=limits.max_total_tokens,
                )
            output = result.output
            if lease.kind == "outline":
                if output.outline is None or output.changes:
                    raise StoryError("invalid_outline_output", 422)
                findings = (
                    prepare_proposal(
                        lease.id,
                        lease.story_id,
                        lease.base_revision,
                        lease.snapshot,
                        output,
                        check_only=True,
                    )
                    if output.findings
                    else None
                )
                await self.store.succeed(lease, findings, output.outline, usage, usage.actual_model)
            else:
                if output.outline is not None:
                    raise StoryError("unexpected_outline", 422)
                proposal = prepare_proposal(
                    lease.id,
                    lease.story_id,
                    lease.base_revision,
                    lease.snapshot,
                    output,
                    check_only=lease.kind == "check",
                    concretize=lease.kind == "concretize",
                )
                await self.store.succeed(lease, proposal, None, usage, usage.actual_model)
        except asyncio.CancelledError:
            # A reserved request is never reclaimed for another physical request.
            await asyncio.shield(
                self.store.fail(
                    lease,
                    "worker_interrupted",
                    usage,
                    complete=usage.complete,
                    actual_model=usage.actual_model,
                )
            )
            raise
        except Exception as error:
            if isinstance(error, (StoryError, AuthoringGenerationError)):
                code = error.code
            elif isinstance(error, TimeoutError):
                code = "job_timeout"
            elif isinstance(error, json.JSONDecodeError):
                code = "invalid_change_json"
            elif isinstance(error, (ValidationError, ValueError)):
                code = "invalid_model_output"
            else:
                code = "model_failed"
            # Exception strings/provider bodies may contain prompts or credentials.
            await self.store.fail(
                lease,
                code,
                usage,
                complete=usage.complete,
                actual_model=usage.actual_model,
            )
        finally:
            # A cancelled/expired result stays fenced, but known billed usage remains visible.
            await self.store.record_usage(
                lease,
                usage,
                complete=usage.complete,
                actual_model=usage.actual_model,
            )
        return True
