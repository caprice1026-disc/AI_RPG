# AI TRPG 型定義とDB契約

版: 0.2 / 2026-09-14 / レビュー用実装案

> **決定の更新:** 本書の暫定値と対象外事項のうち、MVP ruleset、実行時設定、Turn routing、principal境界は [Architecture Decision Records](adr/README.md) で決定済みである。矛盾する場合は採用状態のADRを優先する。

## 1 今回の具体化

合意済みの論理モデルをPython 3.11以降・Pydantic v2とPostgreSQL向けに具体化する。これは技術選定の最終承認や本番へのマイグレーション実行を意味しない。ゲームルール本体、認証基盤、Director、NPC記憶の詳細は対象外。

採用済みの原則は、共通Turn、NarrativeとMechanicalの別LLM契約、型付きIntentとCommandの分離、Eventの任意Action関連、Turn単位のatomicなゲーム確定、確定処理と描写状態の分離である。

追加の実装提案は次の通り。

- Campaign内のゲーム解決はMVPでは一度に1 Turn。LLM待ちの間はDBロックを保持せず、未解決Turnの一意制約で受付を制限する。
- Event StoreはMVPではCanonical DBに併記する追記ログとする。完全なEvent Sourcingは要求しない。
- state_versionはCanonicalの更新で増やす。会話のみ、確認質問、描写再生成では増やさない。ログ順序は別のevent_sequenceを使う。
- Action上限は設定から読み、受付時にTurnへ保存する。既定値3と設定元は [ADR-0008](adr/0008-runtime-defaults.md) で固定し、LLMのJSON Schemaにも同じ設定を反映する。
- 文字数上限など本書で新規に置いた値は初期運用値。ゲームルール上の制限とは別物である。

## 2 Pythonの境界型

以下のコードブロックは独立したPythonモジュールとして読み込める。JSON入力にはmodel_validate_jsonを使う。数値はstrictな整数型を使い、文字列数値やboolを受け付けない。UUIDはJSON文字列から復元する。

LLM出力はextra="forbid"で未定義フィールドを拒否し、kindによるdiscriminated unionで判別する。ただし構造検証は権限検証やゲーム上の正当性検証の代替ではない。

```python
from typing import Annotated, Literal, Self
from uuid import UUID
from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool,
    create_model, model_validator,
)

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SignedInt = Annotated[int, Field(strict=True)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
InputText = Annotated[str, Field(min_length=1, max_length=8000)]
NarrationText = Annotated[str, Field(min_length=1, max_length=12000)]
Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextInput(Contract):
    kind: Literal["text"]
    text: InputText


class ChoiceInput(Contract):
    kind: Literal["choice"]
    choice_id: UUID


PlayerContent = Annotated[TextInput | ChoiceInput, Field(discriminator="kind")]


class PlayerTurnInput(Contract):
    request_id: UUID
    expected_state_version: NonNegativeInt
    actor_id: UUID
    content: PlayerContent


# LLMにはターン内の参照名を渡す。UUIDとの対応表はApplicationが保持する。
class EntityRef(Contract):
    ref: Ref
    label: ShortText
    entity_kind: Literal["pc", "npc", "item", "object"]


class ContextFragment(Contract):
    source: ShortText
    trust_level: Literal["trusted", "derived", "untrusted"]
    access_scope: Literal["public", "actor_private", "gm_private"]
    content: InputText


class OutputLimits(Contract):
    max_actions: PositiveInt = 3
    max_choices: Annotated[int, Field(strict=True, ge=0, le=5)] = 5


class GMInput(Contract):
    player_text: InputText  # ChoiceInputは保存済みの選択文に解決してから渡す
    scene_view: ContextFragment
    pc_view: ContextFragment
    recent_messages: list[ContextFragment] = Field(max_length=20)
    allowed_entity_refs: list[EntityRef] = Field(max_length=100)
    output_limits: OutputLimits


class NarrativeInput(GMInput):
    pass


class MechanicalInput(GMInput):
    supported_action_types: list[Literal["attack", "skill_check", "use_item"]]
    supported_skill_refs: list[Ref]


class AttackIntent(Contract):
    kind: Literal["attack"]
    target_ref: Ref
    weapon_ref: Ref | None  # Noneは非武装。選択が不明なら確認質問を返す


class SkillCheckIntent(Contract):
    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_ref: Ref | None


class UseItemIntent(Contract):
    kind: Literal["use_item"]
    item_ref: Ref
    target_ref: Ref | None


ActionIntent = Annotated[
    AttackIntent | SkillCheckIntent | UseItemIntent,
    Field(discriminator="kind"),
]


class ChoiceDraft(Contract):
    label: ShortText  # 別の隠れた実行文を持たせず、この文を次の入力として扱う


class NarrativeDraft(Contract):
    kind: Literal["narrative"]
    narration: NarrationText
    choices: list[ChoiceDraft] = Field(max_length=5)


class ActionPlan(Contract):
    kind: Literal["action_plan"]
    actions: list[ActionIntent] = Field(min_length=1)


class ResolutionRequired(Contract):
    kind: Literal["resolution_required"]
    actions: list[ActionIntent] = Field(min_length=1)


class ClarificationRequired(Contract):
    kind: Literal["clarification_required"]
    question: ShortText


def make_decision_types(max_actions: int = 3):
    """設定ごとに起動時一度作成。上限はモデル向けJSON Schemaにも含まれる。"""
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    plan = create_model(
        f"ActionPlanLimit{max_actions}", __base__=ActionPlan,
        actions=(list[ActionIntent], Field(min_length=1, max_length=max_actions)),
    )
    escalation = create_model(
        f"ResolutionRequiredLimit{max_actions}", __base__=ResolutionRequired,
        actions=(list[ActionIntent], Field(min_length=1, max_length=max_actions)),
    )
    narrative = Annotated[
        NarrativeDraft | escalation | ClarificationRequired,
        Field(discriminator="kind"),
    ]
    mechanical = Annotated[plan | ClarificationRequired, Field(discriminator="kind")]
    return narrative, mechanical


NarrativeDecision, MechanicalDecision = make_decision_types()


# 以下はApplicationが認証・参照解決・ルール検証をして作る。LLMの出力型にしない。
class CommandBase(Contract):
    action_id: UUID
    campaign_id: UUID
    turn_id: UUID
    actor_id: UUID
    ordinal: PositiveInt


class AttackCommand(CommandBase):
    kind: Literal["attack"]
    target_id: UUID
    weapon_id: UUID | None
    attack_bonus: SignedInt
    damage_expression: ShortText
    damage_bonus: SignedInt


class SkillCheckCommand(CommandBase):
    kind: Literal["skill_check"]
    skill_ref: Ref
    objective: ShortText
    target_id: UUID | None
    modifier: SignedInt
    difficulty_class: PositiveInt


class UseItemCommand(CommandBase):
    kind: Literal["use_item"]
    item_id: UUID
    target_id: UUID | None
    effect_ref: Ref


Command = Annotated[
    AttackCommand | SkillCheckCommand | UseItemCommand,
    Field(discriminator="kind"),
]


class DiceResult(Contract):
    expression: ShortText
    rolls: list[PositiveInt] = Field(min_length=1, max_length=100)
    modifier: SignedInt
    total: SignedInt
    # 個々の出目の範囲・式との整合性は対応rulesetのDiceEngineが検証する


class DamageApplied(Contract):
    kind: Literal["damage_applied"]
    target_id: UUID
    amount: NonNegativeInt
    hp_before: NonNegativeInt
    hp_after: NonNegativeInt


class HealingApplied(Contract):
    kind: Literal["healing_applied"]
    target_id: UUID
    amount: NonNegativeInt
    hp_before: NonNegativeInt
    hp_after: NonNegativeInt
    max_hp: PositiveInt


class ItemConsumed(Contract):
    kind: Literal["item_consumed"]
    owner_id: UUID
    item_id: UUID
    quantity_before: PositiveInt
    quantity_after: NonNegativeInt


StateChange = Annotated[
    DamageApplied | HealingApplied | ItemConsumed,
    Field(discriminator="kind"),
]


class AppliedResult(Contract):
    kind: Literal["applied"]
    outcome: Literal["success", "failure", "neutral"]
    facts: list[ShortText]  # Engineがテンプレートから作る公開可能な確定事実
    dice: list[DiceResult]
    state_changes: list[StateChange]  # Engineが確定した閉じた状態変更


class NotApplicableResult(Contract):
    kind: Literal["not_applicable"]
    reason: Literal["target_unavailable", "resource_unavailable", "rule_precondition"]


ActionResult = Annotated[AppliedResult | NotApplicableResult, Field(discriminator="kind")]


class ResolvedAction(Contract):
    action_id: UUID
    ordinal: PositiveInt
    result: ActionResult


class MechanicalNarrationInput(Contract):
    player_text: InputText
    committed_state_version: NonNegativeInt
    resolved_actions: list[ResolvedAction]
    public_state_after: list[ContextFragment]
    allowed_entity_refs: list[EntityRef]
    output_limits: OutputLimits


class MechanicalNarrationDraft(Contract):
    narration: NarrationText
    choices: list[ChoiceDraft] = Field(max_length=5)


RecoveryReason = Literal[
    "MODEL_TIMEOUT", "INVALID_OUTPUT", "MODEL_REFUSAL",
    "INJECTION_DETECTED", "CONTEXT_CONFLICT", "UNKNOWN",
]


class TurnRecovery(Contract):
    fallback: StrictBool
    reason: RecoveryReason | None

    @model_validator(mode="after")
    def consistent_reason(self) -> Self:
        if self.fallback != (self.reason is not None):
            raise ValueError("fallback and reason must agree")
        return self


class Choice(Contract):
    id: UUID
    label: ShortText


ResolutionStatus = Literal["pending", "resolving", "committed", "not_applied", "failed"]
NarrationStatus = Literal["pending", "generating", "completed", "fallback"]


class TurnResponse(Contract):
    turn_id: UUID
    route: Literal["narrative", "mechanical"] | None  # 分類前のみNone
    resolution_status: ResolutionStatus
    narration_status: NarrationStatus
    committed_state_version: NonNegativeInt | None
    narration: NarrationText | None
    choices: list[Choice]
    action_results: list[ResolvedAction]
    recovery: TurnRecovery

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if (self.resolution_status == "committed") != (self.committed_state_version is not None):
            raise ValueError("committed status requires committed_state_version only")
        if self.resolution_status == "committed" and self.route is None:
            raise ValueError("committed turn requires route")
        if self.action_results and (self.route != "mechanical" or self.resolution_status != "committed"):
            raise ValueError("action results require committed mechanical turn")
        done = self.narration_status in ("completed", "fallback")
        if done != (self.narration is not None):
            raise ValueError("terminal narration requires text only")
        if self.choices and not done:
            raise ValueError("choices require terminal narration")
        if (self.narration_status == "fallback") != self.recovery.fallback:
            raise ValueError("fallback status and recovery must agree")
        return self


class RNGMetadata(Contract):
    source: Literal["secure", "seeded_test", "recorded_replay"]
    implementation_version: ShortText
    draw_index: NonNegativeInt
    # seedそのものは公開イベントに入れない。テストfixtureで保持する。


class DiceRolledPayload(Contract):
    roll: DiceResult
    rng: RNGMetadata


class ActionResolvedPayload(Contract):
    result: ActionResult


class NarrationGeneratedPayload(Contract):
    narration: NarrationText
    fallback: StrictBool


class EventBase(Contract):
    id: UUID
    campaign_id: UUID
    scene_id: UUID | None
    turn_id: UUID | None
    action_id: UUID | None
    sequence: PositiveInt
    state_version: NonNegativeInt
    schema_version: Literal[1]

    @model_validator(mode="after")
    def consistent_parents(self) -> Self:
        if self.turn_id is not None and self.scene_id is None:
            raise ValueError("turn event requires scene")
        if self.action_id is not None and self.turn_id is None:
            raise ValueError("action event requires turn")
        return self


class ActionEventBase(EventBase):
    scene_id: UUID
    turn_id: UUID
    action_id: UUID


class DiceRolledEvent(ActionEventBase):
    type: Literal["DiceRolled"]
    payload: DiceRolledPayload


class DamageAppliedEvent(ActionEventBase):
    type: Literal["DamageApplied"]
    payload: DamageApplied


class HealingAppliedEvent(ActionEventBase):
    type: Literal["HealingApplied"]
    payload: HealingApplied


class ItemConsumedEvent(ActionEventBase):
    type: Literal["ItemConsumed"]
    payload: ItemConsumed


class ActionResolvedEvent(ActionEventBase):
    type: Literal["ActionResolved"]
    payload: ActionResolvedPayload


class NarrationGeneratedEvent(EventBase):
    type: Literal["GMNarrationGenerated"]
    scene_id: UUID
    turn_id: UUID
    action_id: None = None
    payload: NarrationGeneratedPayload


DomainEventV1 = Annotated[
    DiceRolledEvent | DamageAppliedEvent | HealingAppliedEvent |
    ItemConsumedEvent | ActionResolvedEvent | NarrationGeneratedEvent,
    Field(discriminator="type"),
]
```

Provider情報はすべてデータであり、ContextFragmentのcontentをsystem指示として結合しない。access_scopeは公開範囲、trust_levelは出所の信頼区分であり、互いに独立する。MVPでもApplicationは許可していないFragmentをLLMへ渡さない。

ActionResultのfactsは演出用の公開事実であり、Canonical更新命令ではない。正確なダメージ、回復、在庫消費はEngineの型付きStateChangeに記録する。ApplicationはStateChangeをCanonical mutationとEventへ一度だけ投影し、Infrastructureは保存前値をlock下で照合してSQLへ変換する。ルール固有のイベントpayloadは(type, schema_version)ごとの型レジストリで検証する。HPの上下限、技能一覧、攻撃・アイテム・ダイス規則は [ADR-0007](adr/0007-mvp-ruleset.md) の `mvp_v1` に属する。

DomainEventV1は今回具体化した6種の初期型。ActionResolvedは各Actionの最終結果を表し、DiceRolled、DamageApplied、HealingApplied、ItemConsumedは個別の出来事を表す。ActionResolved内のStateChangeと個別Eventを二重適用しない。MVPではEngineの結果を一度だけCanonicalへ適用し、イベントは記録と表示に使う。PlayerMessageAdded、SceneChanged、WorldFactChanged等は各機能実装時に専用型を追加する。Actionに属さない将来イベントはEventBaseから定義できる。

## 3 DBの関連と制約

UUIDはApplicationが発行する。timestampsはtimestamptz、順序はCampaignまたは親Entity内の整数。ログや履歴はON DELETE CASCADEで消さず、アーカイブを基本とする。

campaign_membersとentitiesは参照整合性のための最小補助テーブル。認証ユーザーの実体は外部認証基盤、PC能力値や装備・所持権は別のruleset固有Canonicalテーブルとする。

```sql
BEGIN;

CREATE TABLE campaigns (
    id uuid PRIMARY KEY,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    event_sequence bigint NOT NULL DEFAULT 0 CHECK (event_sequence >= 0),
    ruleset_version text NOT NULL CHECK (length(ruleset_version) > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE campaign_members (
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    principal_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('player','gm')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (campaign_id, principal_id)
);

CREATE TABLE entities (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    kind text NOT NULL CHECK (kind IN ('pc','npc','item','object')),
    controller_id uuid,
    archived_at timestamptz,
    ref text CHECK (ref IS NULL OR ref ~ '^[a-z][a-z0-9_]{0,63}$'),
    label text CHECK (label IS NULL OR length(label) BETWEEN 1 AND 500),
    UNIQUE (campaign_id, id),
    FOREIGN KEY (campaign_id, controller_id)
        REFERENCES campaign_members(campaign_id, principal_id),
    CHECK ((ref IS NULL) = (label IS NULL))
);
CREATE UNIQUE INDEX entities_campaign_ref
    ON entities(campaign_id, ref) WHERE ref IS NOT NULL;

CREATE TABLE scenes (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    status text NOT NULL CHECK (status IN ('planned','active','closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, id),
    UNIQUE (campaign_id, sequence)
);
-- MVP提案: Campaignのactive Sceneは一つ。
CREATE UNIQUE INDEX one_active_scene ON scenes(campaign_id) WHERE status = 'active';

CREATE TABLE turns (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    scene_id uuid NOT NULL,
    request_id uuid NOT NULL,
    created_by uuid NOT NULL,
    actor_id uuid NOT NULL,
    input_schema_version integer NOT NULL DEFAULT 1 CHECK (input_schema_version > 0),
    input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload) = 'object'),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    input_kind text NOT NULL CHECK (input_kind IN ('text','choice')),
    input_text text,
    selected_choice_id uuid,
    expected_state_version bigint NOT NULL CHECK (expected_state_version >= 0),
    committed_state_version bigint CHECK (committed_state_version >= 0),
    route text CHECK (route IN ('narrative','mechanical')),
    resolution_status text NOT NULL DEFAULT 'pending'
        CHECK (resolution_status IN ('pending','resolving','committed','not_applied','failed')),
    narration_status text NOT NULL DEFAULT 'pending'
        CHECK (narration_status IN ('pending','generating','completed','fallback')),
    max_actions integer NOT NULL DEFAULT 3 CHECK (max_actions > 0),
    llm_call_count integer NOT NULL DEFAULT 0 CHECK (llm_call_count BETWEEN 0 AND 3),
    worker_epoch bigint NOT NULL DEFAULT 0 CHECK (worker_epoch >= 0),
    lease_until timestamptz,
    committed_at timestamptz,
    narration text,
    narration_input jsonb CHECK (jsonb_typeof(narration_input) = 'object'),
    recovery_reason text CHECK (recovery_reason IN (
        'MODEL_TIMEOUT','INVALID_OUTPUT','MODEL_REFUSAL',
        'INJECTION_DETECTED','CONTEXT_CONFLICT','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, created_by, request_id),
    UNIQUE (campaign_id, id),
    UNIQUE (campaign_id, scene_id, id),
    FOREIGN KEY (campaign_id, scene_id) REFERENCES scenes(campaign_id, id),
    FOREIGN KEY (campaign_id, created_by) REFERENCES campaign_members(campaign_id, principal_id),
    FOREIGN KEY (campaign_id, actor_id) REFERENCES entities(campaign_id, id),
    CHECK (
        (input_kind = 'text' AND input_text IS NOT NULL
            AND length(input_text) BETWEEN 1 AND 8000 AND selected_choice_id IS NULL)
        OR (input_kind = 'choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL)
    ),
    CHECK ((resolution_status = 'committed') = (committed_state_version IS NOT NULL)),
    CHECK ((resolution_status = 'committed') = (committed_at IS NOT NULL)),
    CHECK (resolution_status <> 'committed' OR route IS NOT NULL),
    CHECK (route IS DISTINCT FROM 'narrative' OR llm_call_count <= 1),
    CHECK ((narration_status IN ('completed','fallback')) = (narration IS NOT NULL)),
    CHECK (narration IS NULL OR length(narration) BETWEEN 1 AND 12000),
    CHECK ((narration_status = 'fallback') = (recovery_reason IS NOT NULL))
);
CREATE UNIQUE INDEX one_unresolved_turn ON turns(campaign_id)
    WHERE resolution_status IN ('pending','resolving');
CREATE INDEX turns_scene_history ON turns(campaign_id, scene_id, created_at, id);

CREATE TABLE turn_choices (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL,
    scene_id uuid NOT NULL,
    source_turn_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 5),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 500),
    state_version bigint NOT NULL CHECK (state_version >= 0),
    invalidated_at timestamptz,
    UNIQUE (source_turn_id, ordinal),
    UNIQUE (campaign_id, scene_id, actor_id, id),
    FOREIGN KEY (campaign_id, scene_id, source_turn_id)
        REFERENCES turns(campaign_id, scene_id, id),
    FOREIGN KEY (campaign_id, actor_id) REFERENCES entities(campaign_id, id)
);
ALTER TABLE turns ADD CONSTRAINT selected_choice_scope
    FOREIGN KEY (campaign_id, scene_id, actor_id, selected_choice_id)
    REFERENCES turn_choices(campaign_id, scene_id, actor_id, id);

CREATE TABLE actions (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL,
    turn_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    actor_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('attack','skill_check','use_item')),
    target_id uuid,
    item_id uuid, -- attackではweapon_id、use_itemではitem_idを投影
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    command jsonb NOT NULL CHECK (jsonb_typeof(command) = 'object'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    result_kind text NOT NULL CHECK (result_kind IN ('applied','not_applicable')),
    ruleset_version text NOT NULL CHECK (length(ruleset_version) > 0),
    UNIQUE (turn_id, ordinal),
    UNIQUE (campaign_id, turn_id, id),
    FOREIGN KEY (campaign_id, turn_id) REFERENCES turns(campaign_id, id),
    FOREIGN KEY (campaign_id, actor_id) REFERENCES entities(campaign_id, id),
    FOREIGN KEY (campaign_id, target_id) REFERENCES entities(campaign_id, id),
    FOREIGN KEY (campaign_id, item_id) REFERENCES entities(campaign_id, id),
    CHECK (kind <> 'attack' OR target_id IS NOT NULL),
    CHECK (kind <> 'use_item' OR item_id IS NOT NULL)
);

-- Actionは確定時だけ挿入する。max_actionsは親Turnから取得する。
CREATE FUNCTION guard_action_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent turns%ROWTYPE;
BEGIN
    SELECT * INTO STRICT parent FROM turns WHERE id = NEW.turn_id FOR UPDATE;
    IF parent.resolution_status <> 'committed'
       OR parent.route IS DISTINCT FROM 'mechanical'
       OR parent.campaign_id <> NEW.campaign_id
       OR parent.actor_id <> NEW.actor_id
       OR NEW.ordinal > parent.max_actions THEN
        RAISE EXCEPTION 'action violates committed turn contract';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER guard_action BEFORE INSERT ON actions
    FOR EACH ROW EXECUTE FUNCTION guard_action_insert();

CREATE TABLE events (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES campaigns(id),
    scene_id uuid,
    turn_id uuid,
    action_id uuid,
    sequence bigint NOT NULL CHECK (sequence > 0),
    state_version bigint NOT NULL CHECK (state_version >= 0),
    type text NOT NULL CHECK (length(type) > 0),
    schema_version integer NOT NULL CHECK (schema_version > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, sequence),
    FOREIGN KEY (campaign_id, scene_id) REFERENCES scenes(campaign_id, id),
    FOREIGN KEY (campaign_id, scene_id, turn_id) REFERENCES turns(campaign_id, scene_id, id),
    FOREIGN KEY (campaign_id, turn_id, action_id) REFERENCES actions(campaign_id, turn_id, id),
    CHECK (turn_id IS NULL OR scene_id IS NOT NULL),
    CHECK (action_id IS NULL OR turn_id IS NOT NULL)
);
CREATE INDEX events_turn ON events(campaign_id, turn_id, sequence) WHERE turn_id IS NOT NULL;
CREATE INDEX events_action ON events(action_id) WHERE action_id IS NOT NULL;

CREATE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'history is append only';
END;
$$;
CREATE TRIGGER immutable_events BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_actions BEFORE UPDATE OR DELETE ON actions
    FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

COMMIT;
```

### DBで守るものとApplicationで守るもの

| 不変条件 | 保証箇所 |
| --- | --- |
| 同じ利用者の同じrequest_idからTurnを二つ作らない | UNIQUE(campaign_id, created_by, request_id) |
| 同じActionと同じordinalを二重保存しない | PK(actions.id)、UNIQUE(turn_id, ordinal) |
| CampaignをまたぐScene、Actor、Target、Itemの参照をしない | campaign_idを含めた複合FK |
| EventのScene／Turn／Actionが同じ親に属する | 複合FKとNULLの依存CHECK |
| active Sceneと未解決Turnは各Campaignに一つ | 部分一意インデックス |
| Actionは確定済みMechanical Turnに属し、上限を超えない | INSERTトリガーとordinalのUNIQUE／CHECK |
| Event／Actionを通常のUPDATE／DELETEで変更しない | 追記専用トリガー |
| committedと確定バージョン・日時が一致する | TurnのCHECK |
| payloadの型、JSONと列の一致 | Pydanticの検証＋単一Repositoryの投影処理 |
| Actorの操作権、Itemの所有、Targetの合法性 | 認証済みprincipalとEngine／Application |
| Actionのordinalが1から連続する、意図された全件が保存される | 確定処理で1..Nと件数を検証 |
| 状態遷移、確定済みTurnの解決内容を変更しない | Applicationの条件付きUPDATE。必要に応じDB権限・トリガーを追加 |
| 状態更新・Action・Event・Turnが同時にcommitされる | 同一DBトランザクションのRepository |
| LLMがDBを書けない | LLMに接続情報や書込toolを渡さない。Application専用接続で仲介 |

FKは認可ではない。会員がactiveか、Actorを操作できるか、Sceneが現在activeかは毎回Applicationで確認する。公開APIからevents.payloadやnarration_inputを丸ごと返さず、認可された公開DTOに変換する。

履歴トリガーはDB所有者やTRUNCATEへの防御を意味しない。本番用接続は非所有者として必要なSELECT／INSERT／限定UPDATEのみ許可し、DDL／TRUNCATE権限を与えない。ここでGRANT対象ロール名はまだ固定しない。

## 4 受付とIdempotency

1. 認証済みprincipalについてCampaign参照権を確認する。
2. (campaign_id, principal_id, request_id)の既存Turnを検索する。再送判定はexpected_state_versionの現在値チェックより先に行う。
3. 既存Turnがある場合、保存済みの正規化入力と照合する。同じ内容なら現在のTurnResponseを返し、違えば409 IDEMPOTENCY_CONFLICT。hashだけでなく保存JSONも比較する。
4. 新規の場合、短いトランザクションでCampaign行をロックし、Scene、Actor権限、state_version、選択肢の有効性を確認する。
5. Turnをpendingとして保存する。別の未解決Turnがあれば409 TURN_IN_PROGRESS。固有制約競合が同じrequest_idによるものなら、rollback後に既存Turnを読み直して手順3を行う。

request_hashは、schema_version、Campaign／Scene、認証principal、正規化したPlayerTurnInputを決定的にシリアライズしたSHA-256とする。UUIDは標準表記、キー順と文字エンコーディングを固定し、入力textの空白や大文字小文字を勝手に変えない。

Choiceは同Campaign・Scene・Actorのものだけ参照できる。さらにinvalidated_at、state_version、提示元Turnを確認する。新しいTurn受付時にそのActorの既存選択肢を無効化する案を採用し、古い会話のボタンから再実行できないようにする。再送は先に既存Turnを返すため、無効化済みChoiceでも元の結果に戻れる。

narrationが遅れて届いた場合、すでに同Actorの後続Turnが存在すれば、その古い描写に新しい有効Choiceを付けない。確認質問への回答は新しいrequest_idを持つ次のTurnとして扱い、失敗した旧Turnを上書きしない。

## 5 ゲーム確定トランザクション

### 計算段階

workerは短いトランザクションでTurnをresolvingへ変更し、worker_epochを加算、lease_untilを設定して所有する。再取得するworkerもepochを加算する。長いLLM待機中に行ロックを保持しない。

同じスナップショットからIntentを検証しCommand列を作る。Action IDはそのTurnのordinalから決定的に割り当てる方式を推奨する。EngineはDBを更新せず、作業用状態を順次更新し、結果とDomain Eventsを返す。

JSONのCommandにはメタデータがあるが、Engineの中核は型付き引数を受ける。LLMから届いたCommand風JSONを直接受理しない。古いTargetや不正なItemなど初期検証で不正な計画はゲームを適用せず、確認またはエラーにする。前のActionの正常結果により後続Actionが実行不能になった場合のみnot_applicableを保存し、それ以降も順に可否を評価する。

### 確定段階

1. Campaign行、次にTurn行をFOR UPDATEで取得する。全更新経路でこのロック順を統一する。
2. すでにcommittedなら既存結果を返す。状態がresolvingで、worker_epochが自身の取得値と一致し、leaseが有効なことを検証する。
3. 現在のstate_versionが計算元と一致することを確認する。不一致なら結果を破棄し、CONTEXT_CONFLICTとして失敗を記録する。黙って新状態で再ロールしない。
4. Canonicalが変化する場合だけstate_versionを1増やし、PC等のCanonicalテーブルを更新する。変更がなければ同じversionを使う。
5. Turnをcommittedへ更新し、committed_at、committed_state_version、描写用の公開スナップショットnarration_inputを保存する。
6. 順序付きActionと結果を全件INSERTする。トリガーのためTurn確定UPDATEを先に実行するが、外部からはcommitまでどちらも見えない。
7. campaigns.event_sequenceを必要件数だけ加算し、その範囲でDomain EventsをINSERTする。
8. commitする。途中のエラーではすべてrollbackする。

Canonical更新を行うDirector等も同じCampaignロックとversion規約を使う。MVPでは未解決Turnが存在する間、Director Proposalの採用を延期する。

DB接続がcommit応答直前に切れた場合、commitが失敗したと決めつけず、まずTurnを再照会する。保存済みなら結果を再利用する。未保存の場合の再試行はworkerの再取得と既存予算内で行う。確定していないダイス結果はプレイヤーへ表示しない。

## 6 Narrativeの確定と描写の復旧

Narrativeの通常完了は0 Actionsのcommitted Turnとなる。Canonicalを変えないのでstate_versionは増やさず、GMNarrationGenerated等のログ用sequenceのみ増やす。

ClarificationRequiredはnot_applied／completedで質問を返す。モデル障害で未適用ならfailed／fallback。判定ミスはcommittedであり、システム失敗ではない。

Mechanicalの描写workerは保存済みnarration_inputだけから再生成する。現在のCanonicalを読み直して過去の結果に混ぜない。ゲーム結果・Actionは再実行しない。

narration_statusはpending → generating → completedまたはfallback。描写完了時は短いトランザクションで文章、Choice、GMNarrationGeneratedを保存する。多重workerによる上書きを防ぐため、条件付きUPDATEとworker所有トークンを用いる。追加migration `0003_turn_worker_controls` は解決用のworker_epoch／lease_untilとは別にnarration_worker_epoch／narration_lease_untilを持ち、古い描写workerの保存を拒否する。

fallbackからの再描写はゲームの再実行ではないが、MVPでは自動の無制限retryはしない。永続化した呼び出し回数を引き継ぎ、予算枠が残る場合のみ一つのworkerで行う。枠がなければ確定事実のテンプレートを最終応答とする。

## 7 呼び出し回数とイベント配信

llm_call_countは実行前に、保存済みllm_call_budget、phase、epoch、lease期限を同時に照合する条件付きUPDATEで予約する。timeoutや無効出力も1回として数え、返却経路は設けない。SDKの自動retryは無効化するか、各実リクエストを同じ予約経路に通す。Directorの呼び出しは別計測。

NarrativeからResolutionRequiredを受け取ったらrouteをmechanicalへ変更し、その1回をIntent抽出として扱う。Narrativeのままrepairを追加しない。MVPでは独立したRouter LLMを置かず、[ADR-0009](adr/0009-turn-routing.md) の安全条件を満たす分類不能入力だけをNarrativeDecisionの昇格で処理する。

state.updatedやdice.rolledなど確定結果のイベントはcommit後だけ配信する。check.started等の進行表示は揮発的でよい。narration.deltaは未確定の文章として扱い、失敗時には確定fallbackに置き換える。

commit後の送信直前に停止しても復旧できるよう、eventsをCampaign内sequenceで読み直して配信する。配信はat-least-onceとし、Clientはevent.idで重複排除、最終sequenceで再接続する。秘密を含むイベントは公開projectionへ変換し、同じrawログを全員に配らない。ストリーム接続ごとの公開範囲とcursorを固定する。narration.deltaは永続リプレイ対象にせず、再接続時は保存済みTurnResponseから復元する。

## 8 最低限の検証項目

| ケース | 期待 |
| --- | --- |
| LLMのattackにdamageやhp_afterが追加される | スキーマで拒否 |
| TextInputとChoiceInputのフィールドを混在させる | スキーマで拒否 |
| 4 ActionsをMVP設定で返す | スキーマで拒否 |
| max_actionsを5に設定する | 5件までのSchemaを生成。過去Turnの上限は変わらない |
| request_idが同じで本文だけ異なる | 409、Action追加なし |
| Campaign AのTurnにCampaign BのActor／Choice／Event関連を使う | FKまたは認可で拒否 |
| 同一Turnのordinalを二重挿入 | UNIQUEで拒否 |
| 古いworkerがlease引継ぎ後にcommitを試みる | epoch不一致で拒否 |
| commit後に描写timeout | ダメージ維持、Action数不変、fallback |
| 二つ目のActionが一つ目の結果で実行不能 | not_applicableを保存しTurnはcommitted |
| イベントINSERT時にDB例外 | Canonical／Action／Turn確定もrollback |
| commit応答喪失後の再送 | 既存Turnを返し再ロールなし |

このDDLは認可やEngineを含む完成したアプリケーションではない。DB実行検証とトランザクション・競合テストを通してからマイグレーション化する。本文のPythonは構造検証用で、特定LLMプロバイダーが受け付けるJSON Schemaサブセットへの変換はアダプターの責務とする。

今回の検証範囲はPythonコードの読み込み、正常入力、未知フィールド・Action数上限・不正な数値型・Recovery整合性の拒否、およびJSON Schema生成。PostgreSQL実行環境がないため、DDLの実行とDB競合テストは未実施。

## 9 参照

Pydanticのkindによるunionと未知フィールドの扱いは、公式の[Unions](https://docs.pydantic.dev/latest/concepts/unions/)と[Models](https://docs.pydantic.dev/latest/concepts/models/)に基づく。DBの複合FK・CHECK・UNIQUE・NULLの扱いはPostgreSQL公式の[Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)を参照。
