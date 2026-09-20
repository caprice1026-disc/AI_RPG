"""PostgreSQL schemaのInfrastructure限定SQLAlchemy mapping。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """AlembicとRepositoryが共有するmapping metadata。"""


class CampaignModel(Base):
    __tablename__ = "campaigns"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    state_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    event_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    ruleset_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("status IN ('active','archived')"),
        CheckConstraint("state_version>=0"),
        CheckConstraint("event_sequence>=0"),
        CheckConstraint("length(ruleset_version)>0"),
    )


class CampaignMemberModel(Base):
    __tablename__ = "campaign_members"

    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), primary_key=True
    )
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (CheckConstraint("role IN ('player','gm')"),)


class PrincipalModel(Base):
    __tablename__ = "principals"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PrincipalIdentityModel(Base):
    __tablename__ = "principal_identities"

    issuer: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principals.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(issuer)>0", name="principal_identity_issuer_nonempty"),
        CheckConstraint("length(subject)>0", name="principal_identity_subject_nonempty"),
        CheckConstraint(
            "disabled_at IS NULL OR disabled_at>=created_at",
            name="principal_identity_disabled_after_created",
        ),
        Index("principal_identities_principal", "principal_id"),
    )


class EntityModel(Base):
    __tablename__ = "entities"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    controller_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ref: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("campaign_id", "id"),
        ForeignKeyConstraint(
            ["campaign_id", "controller_id"],
            ["campaign_members.campaign_id", "campaign_members.principal_id"],
        ),
        CheckConstraint("kind IN ('pc','npc','item','object')"),
        CheckConstraint("(ref IS NULL)=(label IS NULL)", name="entity_ref_pair"),
        CheckConstraint(
            "ref IS NULL OR ref ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="entity_ref_format",
        ),
        CheckConstraint(
            "label IS NULL OR length(label) BETWEEN 1 AND 500",
            name="entity_label_length",
        ),
        Index(
            "entities_campaign_ref",
            "campaign_id",
            "ref",
            unique=True,
            postgresql_where=text("ref IS NOT NULL"),
        ),
    )


class SceneModel(Base):
    __tablename__ = "scenes"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "id"),
        UniqueConstraint("campaign_id", "sequence"),
        CheckConstraint("sequence>0"),
        CheckConstraint("status IN ('planned','active','closed')"),
        Index(
            "one_active_scene",
            "campaign_id",
            unique=True,
            postgresql_where=text("status='active'"),
        ),
    )


class TurnModel(Base):
    __tablename__ = "turns"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    scene_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    input_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    input_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    input_kind: Mapped[str] = mapped_column(Text, nullable=False)
    input_text: Mapped[str | None] = mapped_column(Text)
    selected_choice_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    expected_state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_state_version: Mapped[int | None] = mapped_column(BigInteger)
    route: Mapped[str | None] = mapped_column(Text)
    initial_route: Mapped[str | None] = mapped_column(Text)
    routing_rule_version: Mapped[str | None] = mapped_column(Text)
    routing_reason_codes: Mapped[list[str] | None] = mapped_column(JSONB)
    resolution_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    narration_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    max_actions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    llm_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    llm_call_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    worker_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    resolution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resolution_failure_code: Mapped[str | None] = mapped_column(Text)
    narration_worker_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    narration_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    narration_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration_failure_code: Mapped[str | None] = mapped_column(Text)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration: Mapped[str | None] = mapped_column(Text)
    narration_input: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    recovery_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "created_by", "request_id"),
        UniqueConstraint("campaign_id", "id"),
        UniqueConstraint("campaign_id", "scene_id", "id"),
        ForeignKeyConstraint(
            ["campaign_id", "scene_id"], ["scenes.campaign_id", "scenes.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "created_by"],
            ["campaign_members.campaign_id", "campaign_members.principal_id"],
        ),
        ForeignKeyConstraint(
            ["campaign_id", "actor_id"], ["entities.campaign_id", "entities.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "scene_id", "actor_id", "selected_choice_id"],
            [
                "turn_choices.campaign_id",
                "turn_choices.scene_id",
                "turn_choices.actor_id",
                "turn_choices.id",
            ],
            name="selected_choice_scope",
            use_alter=True,
        ),
        CheckConstraint("input_schema_version>0"),
        CheckConstraint("jsonb_typeof(input_payload)='object'"),
        CheckConstraint("octet_length(request_hash)=32"),
        CheckConstraint("input_kind IN ('text','choice')"),
        CheckConstraint("expected_state_version>=0"),
        CheckConstraint("committed_state_version>=0"),
        CheckConstraint("route IN ('narrative','mechanical')"),
        CheckConstraint("initial_route IN ('narrative','mechanical')"),
        CheckConstraint(
            "routing_rule_version IS NULL OR length(routing_rule_version) BETWEEN 1 AND 100"
        ),
        CheckConstraint(
            "routing_reason_codes IS NULL OR jsonb_typeof(routing_reason_codes)='array'"
        ),
        CheckConstraint(
            "(initial_route IS NULL)="
            "(routing_rule_version IS NULL AND routing_reason_codes IS NULL)",
            name="routing_metadata_complete",
        ),
        CheckConstraint(
            "resolution_status IN ('pending','resolving','committed','not_applied','failed')"
        ),
        CheckConstraint("narration_status IN ('pending','generating','completed','fallback')"),
        CheckConstraint("max_actions>0"),
        CheckConstraint("llm_call_count BETWEEN 0 AND 3"),
        CheckConstraint("llm_call_budget BETWEEN 1 AND 3"),
        CheckConstraint("llm_call_count<=llm_call_budget"),
        CheckConstraint("worker_epoch>=0"),
        CheckConstraint("resolution_attempt_count>=0"),
        CheckConstraint("narration_worker_epoch>=0"),
        CheckConstraint("narration_attempt_count>=0"),
        CheckConstraint("(resolution_started_at IS NULL)=(resolution_deadline IS NULL)"),
        CheckConstraint("(narration_started_at IS NULL)=(narration_deadline IS NULL)"),
        CheckConstraint(
            "(input_kind='text' AND input_text IS NOT NULL AND length(input_text) "
            "BETWEEN 1 AND 8000 AND selected_choice_id IS NULL) OR "
            "(input_kind='choice' AND input_text IS NULL AND selected_choice_id IS NOT NULL)"
        ),
        CheckConstraint("(resolution_status='committed')=(committed_state_version IS NOT NULL)"),
        CheckConstraint("(resolution_status='committed')=(committed_at IS NOT NULL)"),
        CheckConstraint("resolution_status<>'committed' OR route IS NOT NULL"),
        CheckConstraint("route IS DISTINCT FROM 'narrative' OR llm_call_count<=1"),
        CheckConstraint("(narration_status IN ('completed','fallback'))=(narration IS NOT NULL)"),
        CheckConstraint("narration IS NULL OR length(narration) BETWEEN 1 AND 12000"),
        CheckConstraint("(narration_status='fallback')=(recovery_reason IS NOT NULL)"),
        Index(
            "one_open_turn",
            "campaign_id",
            unique=True,
            postgresql_where=text(
                "resolution_status IN ('pending','resolving') "
                "OR narration_status IN ('pending','generating')"
            ),
        ),
        Index("turns_scene_history", "campaign_id", "scene_id", "created_at", "id"),
    )


class TurnChoiceModel(Base):
    __tablename__ = "turn_choices"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scene_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source_turn_id", "ordinal"),
        UniqueConstraint("campaign_id", "scene_id", "actor_id", "id"),
        ForeignKeyConstraint(
            ["campaign_id", "scene_id", "source_turn_id"],
            ["turns.campaign_id", "turns.scene_id", "turns.id"],
        ),
        ForeignKeyConstraint(
            ["campaign_id", "actor_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 5"),
        CheckConstraint("length(label) BETWEEN 1 AND 500"),
        CheckConstraint("state_version>=0"),
    )


class ActionModel(Base):
    __tablename__ = "actions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    item_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    command: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    result_kind: Mapped[str] = mapped_column(Text, nullable=False)
    ruleset_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("turn_id", "ordinal"),
        UniqueConstraint("campaign_id", "turn_id", "id"),
        ForeignKeyConstraint(
            ["campaign_id", "turn_id"], ["turns.campaign_id", "turns.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "actor_id"], ["entities.campaign_id", "entities.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "target_id"], ["entities.campaign_id", "entities.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "item_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("ordinal>0"),
        CheckConstraint("kind IN ('attack','skill_check','use_item')"),
        CheckConstraint("schema_version>0"),
        CheckConstraint("jsonb_typeof(command)='object'"),
        CheckConstraint("jsonb_typeof(result)='object'"),
        CheckConstraint("result_kind IN ('applied','not_applicable')"),
        CheckConstraint("length(ruleset_version)>0"),
        CheckConstraint("kind<>'attack' OR target_id IS NOT NULL"),
        CheckConstraint("kind<>'use_item' OR item_id IS NOT NULL"),
    )


class EventModel(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=False
    )
    scene_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    turn_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    action_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "sequence"),
        ForeignKeyConstraint(
            ["campaign_id", "scene_id"], ["scenes.campaign_id", "scenes.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "scene_id", "turn_id"],
            ["turns.campaign_id", "turns.scene_id", "turns.id"],
        ),
        ForeignKeyConstraint(
            ["campaign_id", "turn_id", "action_id"],
            ["actions.campaign_id", "actions.turn_id", "actions.id"],
        ),
        CheckConstraint("sequence>0"),
        CheckConstraint("state_version>=0"),
        CheckConstraint("length(type)>0"),
        CheckConstraint("schema_version>0"),
        CheckConstraint("jsonb_typeof(payload)='object'"),
        CheckConstraint("turn_id IS NULL OR scene_id IS NOT NULL"),
        CheckConstraint("action_id IS NULL OR turn_id IS NOT NULL"),
        Index(
            "events_turn",
            "campaign_id",
            "turn_id",
            "sequence",
            postgresql_where=text("turn_id IS NOT NULL"),
        ),
        Index(
            "events_action",
            "action_id",
            postgresql_where=text("action_id IS NOT NULL"),
        ),
    )


class MvpCharacterModel(Base):
    __tablename__ = "mvp_characters"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    current_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    defense: Mapped[int] = mapped_column(Integer, nullable=False)
    attack_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "entity_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("defense>=0"),
        CheckConstraint("max_hp>=1"),
        CheckConstraint("current_hp BETWEEN 0 AND max_hp"),
    )


class MvpSkillModifierModel(Base):
    __tablename__ = "mvp_skill_modifiers"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    character_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    skill_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    modifier: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "character_id"],
            ["mvp_characters.campaign_id", "mvp_characters.entity_id"],
        ),
        CheckConstraint(
            "skill_ref IN ('athletics','acrobatics','perception','stealth','persuasion')"
        ),
    )


class MvpWeaponModel(Base):
    __tablename__ = "mvp_weapons"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    damage_expression: Mapped[str] = mapped_column(Text, nullable=False)
    damage_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "entity_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("damage_expression ~ '^[0-9]+d[0-9]+([+-][0-9]+)?$'"),
    )


class MvpInventoryModel(Base):
    __tablename__ = "mvp_inventory"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    equipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "owner_id"],
            ["mvp_characters.campaign_id", "mvp_characters.entity_id"],
        ),
        ForeignKeyConstraint(
            ["campaign_id", "item_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("quantity>=0"),
        Index(
            "one_equipped_weapon",
            "campaign_id",
            "owner_id",
            unique=True,
            postgresql_where=text("equipped"),
        ),
    )


class MvpSceneEntityModel(Base):
    __tablename__ = "mvp_scene_entities"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scene_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_attack_reachable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "scene_id"], ["scenes.campaign_id", "scenes.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "entity_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint(
            "NOT is_attack_reachable OR is_public",
            name="scene_entity_reachable_is_public",
        ),
    )


class MvpScenarioRunModel(Base):
    __tablename__ = "mvp_scenario_runs"

    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("campaigns.id"), primary_key=True
    )
    scenario_ref: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    ending_ref: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("scenario_ref ~ '^[a-z][a-z0-9_]{0,63}$'"),
        CheckConstraint("scenario_version > 0"),
        CheckConstraint("status IN ('active','completed')"),
        CheckConstraint("(status='completed') = (ending_ref IS NOT NULL)"),
        CheckConstraint(
            "ending_ref IS NULL OR ending_ref ~ '^[a-z][a-z0-9_]{0,63}$'"
        ),
    )


class MvpScenarioFlagModel(Base):
    __tablename__ = "mvp_scenario_flags"

    campaign_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("mvp_scenario_runs.campaign_id"),
        primary_key=True,
    )
    flag_ref: Mapped[str] = mapped_column(Text, primary_key=True)

    __table_args__ = (CheckConstraint("flag_ref ~ '^[a-z][a-z0-9_]{0,63}$'"),)


class MvpSceneSkillCheckModel(Base):
    __tablename__ = "mvp_scene_skill_checks"

    campaign_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scene_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    check_ref: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_ref: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    public_description: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "scene_id"], ["scenes.campaign_id", "scenes.id"]
        ),
        ForeignKeyConstraint(
            ["campaign_id", "target_id"], ["entities.campaign_id", "entities.id"]
        ),
        CheckConstraint("check_ref ~ '^[a-z][a-z0-9_]{0,63}$'"),
        CheckConstraint(
            "skill_ref IN ('athletics','acrobatics','perception','stealth','persuasion')"
        ),
        CheckConstraint("difficulty IN ('easy','normal','hard')"),
        CheckConstraint("length(public_description) BETWEEN 1 AND 2000"),
    )
