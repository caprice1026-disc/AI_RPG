# Ruined Chapel Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Fake LLMと実PostgreSQLで、廃礼拝堂Scenarioを入口から三種類の結末まで安全に完走できるようにする。

**Architecture:** 型付きJSONをScenario定義の正本とし、DBにはrunとflagだけを保存する。Applicationが登録済み行動とEngine結果からScenarioProgressUpdateを作り、既存のMechanical commitがAction、Canonical更新、Scene遷移、Event、Turnを一つのtransactionで確定する。

**Tech Stack:** Python 3.11+、Pydantic 2、FastAPI、SQLAlchemy 2、PostgreSQL、Alembic、pytest、既存Fake LLM

## Global Constraints

- 設計正本は docs/superpowers/specs/2026-09-20-ruined-chapel-scenario-design.md とする。
- Narrative commitはCanonical Stateを変更しない。Scenario変更はMechanical commitだけで行う。
- LLMは登録済みaction_refを提案するだけで、Scene ID、flag、endingを指定しない。
- Engineはダイス、攻撃、回復を担当し、Scenario遷移条件を持たない。
- Scenarioなしの既存Campaignでは現在のTurn、API、worker挙動を維持する。
- 同じrequest_idの再送判定を、完了済みScenarioの新規受付拒否より先に行う。
- Scene遷移、flag、ending、Action、Event、Turn確定は同一transactionとする。
- 描写再試行でScenario進行を再適用しない。
- 新しい依存、汎用workflow engine、開始画面、実LLM、敵反撃を追加しない。
- 各production変更は、対応するtestを先に失敗させてから実装する。
- 実装は隔離worktreeで行う。以下の `.\\.venv\\Scripts\\python.exe` と `.\\.venv\\Scripts\\uv.exe` は、共有環境 `C:\\Users\\Hodaka\\Downloads\\div\\AI_RPG\\.venv\\Scripts\\python.exe` と `C:\\Users\\Hodaka\\Downloads\\div\\AI_RPG\\.venv\\Scripts\\uv.exe` に読み替える。

---

### Task 1: 型付きScenario定義と組込みcatalog

**Files:**
- Create: src/ai_rpg/scenarios/__init__.py
- Create: src/ai_rpg/scenarios/models.py
- Create: src/ai_rpg/scenarios/catalog.py
- Create: src/ai_rpg/scenarios/ruined_chapel.json
- Create: tests/unit/test_scenarios.py

**Interfaces:**
- Produces: ScenarioDefinition、SceneDefinition、ScenarioActionDefinition、ScenarioEffect、ScenarioFlagDefinition、EndingDefinition
- Produces: ScenarioCatalog.get(scenario_ref: str, version: int) -> ScenarioDefinition
- Produces: BUILTIN_SCENARIOS
- Consumes: Pydantic Contract、importlib.resources

- [ ] **Step 1: 有効定義と不正定義の失敗testを書く**

    from ai_rpg.scenarios import BUILTIN_SCENARIOS, ScenarioDefinition

    def test_builtin_ruined_chapel_is_typed_and_closed() -> None:
        scenario = BUILTIN_SCENARIOS.get("ruined_chapel", 1)
        assert scenario.title == "廃礼拝堂の聖印"
        assert [scene.scene_ref for scene in scenario.scenes] == [
            "entrance", "hall", "sanctum"
        ]
        assert {ending.ending_ref for ending in scenario.endings} == {
            "recovered", "costly_success", "retreated"
        }

    @pytest.mark.parametrize(
        "change",
        ["duplicate_scene", "dangling_scene", "invalid_skill", "effect_conflict"],
    )
    def test_scenario_definition_rejects_invalid_graph(change: str) -> None:
        payload = invalid_payload(change)
        with pytest.raises(ValidationError):
            ScenarioDefinition.model_validate(payload)

期待値は手書きliteralを使い、production loaderでexpected値を組み立てない。

- [ ] **Step 2: testを実行し、Scenario package不存在で失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_scenarios.py

Expected: FAIL with ModuleNotFoundError for ai_rpg.scenarios。

- [ ] **Step 3: 最小の型付き定義とcatalogを実装する**

models.pyには次の判別可能なaction型を置く。

    class ScenarioEffect(Contract):
        next_scene_ref: Ref | None = None
        ending_ref: Ref | None = None
        add_flags: list[Ref] = Field(default_factory=list)

    class DirectScenarioAction(Contract):
        action_ref: Ref
        label: ShortText
        public_fact: ShortText
        kind: Literal["scenario_action"]
        success: ScenarioEffect

    class SkillScenarioAction(Contract):
        action_ref: Ref
        label: ShortText
        kind: Literal["skill_check"]
        check_ref: Ref
        skill_ref: Ref
        success: ScenarioEffect
        failure: ScenarioEffect

    class AttackScenarioAction(Contract):
        action_ref: Ref
        label: ShortText
        kind: Literal["attack"]
        target_ref: Ref
        defeated: ScenarioEffect

ScenarioDefinitionのafter validatorで、scene sequence、全refの一意性、遷移先、ending、skill_ref、effectのnext/ending排他を一度に検証する。catalog.pyはimportlib.resources.filesでJSONを読み、(scenario_ref, version)をkeyにした変更不能なmappingを保持する。

ScenarioDefinitionはobjective、Sceneのtitle/description/actions、flagのflag_ref/public_fact、Endingのending_ref/title/summaryを型付きで保持する。公開Contextはflag_refそのものではなくScenarioFlagDefinition.public_factを使う。

ruined_chapel.jsonには次を確定値として記録する。

- entrance: enter_chapel -> hall
- hall: search_hall/perception/normal
- search success: clue_found + sanctum
- search failure: alerted + sanctum
- sanctum: negotiate_guard/persuasion、sneak_to_relic/stealth、defeat_guard/attack、retreat
- endings: recovered、costly_success、retreated

- [ ] **Step 4: unit testを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_scenarios.py

Expected: all passed。

- [ ] **Step 5: package buildにJSONが入ることを確認するtestを追加する**

testはimportlib.resources.files("ai_rpg.scenarios").joinpath("ruined_chapel.json").read_text()を実行し、scenario_refのliteralをassertする。source文字列のgrepではなく、実際のpackage resource APIを使う。

- [ ] **Step 6: Task 1をコミットする**

    git add src/ai_rpg/scenarios tests/unit/test_scenarios.py
    git commit -m "feat: define ruined chapel scenario"

---

### Task 2: Scenario action、進行更新、Domain Eventの型契約

**Files:**
- Modify: src/ai_rpg/contracts/llm_decisions.py
- Modify: src/ai_rpg/contracts/context.py
- Modify: src/ai_rpg/domain/commands.py
- Modify: src/ai_rpg/domain/events.py
- Modify: src/ai_rpg/application/ports/repositories.py
- Modify: src/ai_rpg/application/resolution.py
- Modify: src/ai_rpg/contracts/__init__.py
- Modify: src/ai_rpg/domain/__init__.py
- Test: tests/unit/test_contracts.py
- Test: tests/unit/test_resolution_projection.py

**Interfaces:**
- Produces: ScenarioActionIntent(kind="scenario_action", action_ref)
- Produces: ScenarioActionCommand
- Produces: ScenarioProgressUpdate
- Produces: ScenarioProgressedEvent
- Changes: CommitBundle.scenario_update: ScenarioProgressUpdate | None = None
- Changes: MechanicalInput.supported_action_types includes scenario_action
- Changes: ResolutionProjection includes scenario_update and counts it as a state change

- [ ] **Step 1: contractとprojectionの失敗testを書く**

    def test_scenario_action_is_a_typed_intent() -> None:
        _, adapter = make_decision_types(3)
        decision = adapter.validate_python({
            "kind": "action_plan",
            "actions": [{"kind": "scenario_action", "action_ref": "enter_chapel"}],
        })
        assert decision.actions[0].action_ref == "enter_chapel"

    def test_scenario_progress_increments_version_and_emits_event() -> None:
        bundle = CommitBundle(
            ...,
            actions=(scenario_action_record(),),
            scenario_update=ScenarioProgressUpdate(
                from_scene_id=SCENE_ENTRANCE,
                to_scene_id=SCENE_HALL,
                add_flags=(),
                ending_ref=None,
            ),
            narration_input=narration_input(committed_state_version=4),
        )
        projection = project_resolution(bundle, turn_context(), first_event_sequence=8)
        assert projection.committed_state_version == 4
        assert projection.events[-1].type == "ScenarioProgressed"

base_state_versionは3、Canonical mutationは空にし、Scenario更新だけでversion 4になることを確認する。

- [ ] **Step 2: focused testsが新しい型の不存在で失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_contracts.py tests/unit/test_resolution_projection.py

Expected: FAIL importing ScenarioActionIntentまたはScenarioProgressUpdate。

- [ ] **Step 3: 型とprojectionを最小実装する**

ScenarioActionCommandはCommandBaseを継承しaction_refだけを追加する。

    class ScenarioActionCommand(CommandBase):
        kind: Literal["scenario_action"]
        action_ref: Ref

ScenarioProgressUpdateはInfrastructure非依存のdataclassとする。

    @dataclass(frozen=True, slots=True)
    class ScenarioProgressUpdate:
        from_scene_id: UUID
        to_scene_id: UUID | None
        add_flags: tuple[str, ...]
        ending_ref: str | None

ScenarioProgressedPayloadはfrom/to Scene UUID、追加flag、endingを持つ。ScenarioProgressedEventはTurnに紐づく最後のEventとしてproject_resolutionが生成し、action_idはNoneとする。

version計算は次の一箇所へ集約する。

    changed = bool(canonical_mutations) or bundle.scenario_update is not None
    version = bundle.base_state_version + int(changed)

Scenario更新がある場合、narration_input.committed_state_versionも同じversionでなければbundleを拒否する。

- [ ] **Step 4: focused testsとcontract testsを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_contracts.py tests/unit/test_resolution_projection.py tests/contract/test_contract_validation.py

Expected: all passed。

- [ ] **Step 5: Task 2をコミットする**

    git add src/ai_rpg/contracts src/ai_rpg/domain src/ai_rpg/application/ports/repositories.py src/ai_rpg/application/resolution.py tests/unit/test_contracts.py tests/unit/test_resolution_projection.py
    git commit -m "feat: add typed scenario progress contracts"

---

### Task 3: Scenario run schema、ORM、snapshot

**Files:**
- Create: migrations/versions/0009_scenario_progress.py
- Modify: src/ai_rpg/infrastructure/postgres/models.py
- Modify: src/ai_rpg/infrastructure/postgres/repositories.py
- Modify: src/ai_rpg/application/ports/repositories.py
- Modify: tests/unit/test_postgres_models.py
- Modify: tests/integration/postgres/test_migrations.py

**Interfaces:**
- Produces: MvpScenarioRunModel、MvpScenarioFlagModel
- Produces: ScenarioRunSnapshot、ScenarioSceneSnapshot
- Produces: ScenarioRepository.snapshot(campaign_id)
- Changes: RepositorySet.scenarios
- Changes: CanonicalSnapshot.scenario_run
- Changes: PostgresUnitOfWork.scenarios

- [ ] **Step 1: ORM metadataとmigrationの失敗testを書く**

    def test_scenario_progress_metadata_matches_constraints() -> None:
        run = models.MvpScenarioRunModel.__table__
        flags = models.MvpScenarioFlagModel.__table__
        assert list(run.primary_key.columns.keys()) == ["campaign_id"]
        assert {"scenario_ref", "scenario_version", "status", "ending_ref"} <= set(run.c)
        assert list(flags.primary_key.columns.keys()) == ["campaign_id", "flag_ref"]

PostgreSQL testではAlembic head後に二表が存在し、active+ending、completed+NULL ending、無効refをDB constraintが拒否することを確認する。

- [ ] **Step 2: testを実行し、新migrationとmodel不存在で失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_postgres_models.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_progress"

Expected: FAIL because MvpScenarioRunModel and tables do not exist。

- [ ] **Step 3: migrationとORMを実装する**

0009は次の制約を持つ。

    CHECK (scenario_ref ~ '^[a-z][a-z0-9_]{0,63}$')
    CHECK (scenario_version > 0)
    CHECK (status IN ('active','completed'))
    CHECK ((status='completed') = (ending_ref IS NOT NULL))
    CHECK (ending_ref IS NULL OR ending_ref ~ '^[a-z][a-z0-9_]{0,63}$')
    CHECK (flag_ref ~ '^[a-z][a-z0-9_]{0,63}$')

downgradeはflag表、run表の順に削除する。backfillは行わない。

- [ ] **Step 4: Scenario snapshotを実装する**

ScenarioRunSnapshotはrun、同Campaignの全Scene id/sequence/status、flag集合を持つ。

    @dataclass(frozen=True, slots=True)
    class ScenarioSceneSnapshot:
        id: UUID
        sequence: int
        status: str

    @dataclass(frozen=True, slots=True)
    class ScenarioRunSnapshot:
        campaign_id: UUID
        scenario_ref: str
        scenario_version: int
        status: str
        ending_ref: str | None
        scenes: tuple[ScenarioSceneSnapshot, ...]
        flags: frozenset[str]

PostgresScenarioRepository.snapshotはrunがないCampaignでNoneを返す。PostgresCanonicalRepository.snapshotは同じread transaction内でScenario snapshotを取得し、既存fixtureではNoneを返す。

- [ ] **Step 5: unitとPostgreSQL testsを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_postgres_models.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_progress or canonical_snapshot"

Expected: all selected tests passed。

- [ ] **Step 6: Task 3をコミットする**

    git add migrations/versions/0009_scenario_progress.py src/ai_rpg/infrastructure/postgres src/ai_rpg/application/ports/repositories.py tests/unit/test_postgres_models.py tests/integration/postgres/test_migrations.py
    git commit -m "feat: persist scenario progress"

---

### Task 4: 純粋なScenario進行評価

**Files:**
- Create: src/ai_rpg/application/scenarios.py
- Modify: src/ai_rpg/application/__init__.py
- Create: tests/unit/test_scenario_progression.py

**Interfaces:**
- Consumes: ScenarioCatalog、ScenarioRunSnapshot、Scenario Action結果
- Produces: ScenarioPublicContext
- Produces: ScenarioProgressor.definition_for(snapshot)
- Produces: ScenarioProgressor.bind_scenario_action、bind_skill_check、bind_attack
- Produces: ScenarioProgressor.progress_for(snapshot, binding, outcome, target_hp_after)
- Produces: ScenarioProgressUpdateまたはNone

- [ ] **Step 1: 進行表の失敗testを書く**

手書きfixtureで次を個別にassertする。

    def test_search_failure_adds_alerted_and_advances() -> None:
        progress = progressor.progress_for(
            hall_snapshot(),
            binding=skill_binding("search_hall"),
            outcome="failure",
            target_hp_after=None,
        )
        assert progress.add_flags == ("alerted",)
        assert progress.to_scene_id == SANCTUM_ID
        assert progress.ending_ref is None

    def test_alerted_persuasion_success_is_costly_success() -> None:
        progress = progressor.progress_for(
            sanctum_snapshot(flags={"alerted"}),
            binding=skill_binding("negotiate_guard"),
            outcome="success",
            target_hp_after=None,
        )
        assert progress.ending_ref == "costly_success"

同じfileでenter、search success、stealth failure、attack HP>0、attack HP=0、retreat、未登録action、version mismatchを検証する。

- [ ] **Step 2: testがApplication module不存在で失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_scenario_progression.py

Expected: FAIL importing ai_rpg.application.scenarios。

- [ ] **Step 3: ScenarioProgressorを最小実装する**

ScenarioProgressorはDBやEngineをimportしない。Scene sequenceから現在定義と遷移先UUIDを解決し、effectをScenarioProgressUpdateへ変換する。

公開Contextは次の型で返す。

    @dataclass(frozen=True, slots=True)
    class ScenarioPublicContext:
        scene_title: str
        scene_description: str
        objective: str
        discovered_facts: tuple[str, ...]
        available_actions: tuple[tuple[str, str], ...]

raw flag名は公開せず、Scenario定義のpublic fact文字列へ変換する。異なるscenario version、active Scene不在、sequence不一致はScenarioStateErrorとする。

- [ ] **Step 4: unit testを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_scenario_progression.py

Expected: all passed。

- [ ] **Step 5: Task 4をコミットする**

    git add src/ai_rpg/application/scenarios.py src/ai_rpg/application/__init__.py tests/unit/test_scenario_progression.py
    git commit -m "feat: evaluate scenario progression"

---

### Task 5: Scenario進行のatomic commitと完了後受付拒否

**Files:**
- Modify: src/ai_rpg/infrastructure/postgres/repositories.py
- Modify: src/ai_rpg/application/ports/repositories.py
- Modify: src/ai_rpg/application/__init__.py
- Modify: src/ai_rpg/api/app.py
- Modify: tests/integration/postgres/test_migrations.py
- Modify: tests/integration/test_api.py

**Interfaces:**
- Produces: AdventureCompletedError(code="ADVENTURE_COMPLETED")
- Changes: PostgresTurnRepository.commit_resolution applies ScenarioProgressUpdate
- Preserves: existing request replay before completed-run rejection

- [ ] **Step 1: atomicityとidempotenceの失敗testを書く**

PostgreSQL testsはdirect CommitBundleで次を検証する。

- scenario_actionだけでstate_versionが1増える。
- from Sceneがclosed、to Sceneがactiveになる。
- flagとScenarioProgressed Eventが一件追加される。
- endingではcurrent Sceneがclosed、runがcompletedになる。
- Event INSERTを故意に失敗させるとAction、Scene、flag、run、Campaign version、Turnがすべてrollbackする。
- committed Turnを再度commitしても行数が増えない。
- stale epoch、old version、from Scene不一致を拒否する。
- 既存request_idはcompleted後もreplayでき、新しいrequest_idだけADVENTURE_COMPLETEDになる。

- [ ] **Step 2: focused PostgreSQL/API testsが失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_commit or completed_adventure"
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/test_api.py -k "adventure_completed"

Expected: FAIL because progress update is not persisted and error is not mapped。

- [ ] **Step 3: Repository commitを実装する**

commit_resolutionはCampaign、Turnを既存順序でlockした後、scenario_updateがある場合だけrunとactive Sceneをlockする。

検証順:

1. run statusがactive
2. active Sceneがfrom_scene_id
3. to_scene_idがある場合は同Campaignのplanned Scene
4. ending_refがある場合はto_scene_idがNone
5. update後もactive Scene一件以下

projection.committed_state_versionとeventsを正本にし、Repositoryでversionやevent countを再計算しない。flagはINSERTし、Scene/run更新後にActionとEventを既存順序で保存する。

- [ ] **Step 4: 完了後受付拒否を実装する**

PostgresTurnRepository.addは既存request_idのreplay判定後、新規受付だけrun statusを確認する。completedならAdventureCompletedErrorを送出する。FastAPIは既存の409 Application error群と同じ形式で返す。

- [ ] **Step 5: focused testsを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_commit or completed_adventure"
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/test_api.py -k "adventure_completed"

Expected: all selected tests passed。

- [ ] **Step 6: Task 5をコミットする**

    git add src/ai_rpg/infrastructure/postgres/repositories.py src/ai_rpg/application src/ai_rpg/api/app.py tests/integration/postgres/test_migrations.py tests/integration/test_api.py
    git commit -m "feat: commit scenario progress atomically"

---

### Task 6: Worker、Context、Development Fakeの接続

**Files:**
- Modify: src/ai_rpg/application/workers.py
- Modify: src/ai_rpg/application/routing.py
- Modify: src/ai_rpg/llm/fake.py
- Modify: src/ai_rpg/runtime.py
- Modify: tests/unit/test_structured_llm.py
- Modify: tests/unit/test_runtime.py
- Modify: tests/integration/postgres/test_migrations.py

**Interfaces:**
- Changes: SkillCheckResolutionWorker receives ScenarioProgressor
- Changes: Action preflight accepts ScenarioActionIntent
- Changes: public Scene Context comes from ScenarioPublicContext when a run exists
- Changes: DevelopmentFakeTransport maps fixed Japanese inputs to registered actions

- [ ] **Step 1: worker preflightとContextの失敗testを書く**

testsは次をassertする。

- Scenario runでは固定文ではなく現在Scene、目的、公開fact、available actionがinput_dataに入る。
- hidden ending条件、別Scene説明、生のflag名は入らない。
- 未登録scenario_actionはEngine/RNG、Action、Event、progressを一切変更しない。
- 二つの進行actionを含むplanは最初のRNG前に拒否される。
- enter_chapelとretreatはdiceなしAppliedResultになる。
- search skill outcomeとgoblin HP 0でScenarioProgressUpdateがbundleへ入る。
- Scenarioなしsnapshotでは従来の固定ContextとAction処理が維持される。

- [ ] **Step 2: focused testsが失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_structured_llm.py tests/unit/test_runtime.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_worker"

Expected: FAIL because worker does not support scenario_action or Scenario Context。

- [ ] **Step 3: workerへScenarioProgressorを接続する**

全Intentの静的検証を最初に行い、Scenario進行候補が二件以上ならResolutionInputErrorにする。ScenarioActionCommandのresultは次を使う。

    AppliedResult(
        kind="applied",
        outcome="neutral",
        facts=[registered_action.public_fact],
        dice=[],
        state_changes=[],
    )

Skill checkとattackはEngine結果確定後にprogressorへ渡す。Scenario updateがある場合、public_state_afterへ次SceneまたはEndingの公開文をtrusted/derived dataとして追加し、narration_inputのversionを1増やす。

- [ ] **Step 4: ContextとFakeを更新する**

MechanicalInput.supported_action_typesへscenario_actionを追加し、available action refsを公開Scene Contextへ含める。RuleBasedTurnRouterが入る、調べる、交渉、隠密、撤退を状態変更候補としてmechanicalへ送るか、NarrativeのResolutionRequiredで同じIntentへ昇格することをtestで固定する。

DevelopmentFakeTransportはplayer_textと現在のavailable actionだけを見て、設計書の六種類のIntentを返す。利用できないactionを勝手に返さない。

- [ ] **Step 5: focused testsを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_structured_llm.py tests/unit/test_runtime.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "scenario_worker"

Expected: all selected tests passed。

- [ ] **Step 6: Task 6をコミットする**

    git add src/ai_rpg/application src/ai_rpg/llm/fake.py src/ai_rpg/runtime.py tests/unit/test_structured_llm.py tests/unit/test_runtime.py tests/integration/postgres/test_migrations.py
    git commit -m "feat: connect scenario progression worker"

---

### Task 7: 開発fixtureと公開Adventure state

**Files:**
- Modify: src/ai_rpg/runtime.py
- Modify: src/ai_rpg/contracts/responses.py
- Modify: src/ai_rpg/contracts/__init__.py
- Modify: src/ai_rpg/application/turn_queries.py
- Modify: src/ai_rpg/api/app.py
- Modify: src/ai_rpg/cli.py
- Modify: tests/unit/test_runtime.py
- Modify: tests/unit/test_turn_queries.py
- Modify: tests/integration/test_api.py
- Modify: tests/integration/postgres/test_migrations.py

**Interfaces:**
- Produces: AdventureScene、AdventureAction、AdventureEnding、AdventureState
- Changes: CampaignStateResponse.adventure: AdventureState | None = None
- Changes: DevelopmentFixture exposes entrance_scene_id、hall_scene_id、sanctum_scene_id
- Preserves: DevelopmentFixture.scene_id as entrance_scene_id compatibility alias only if existing callers require it

- [ ] **Step 1: seedとpublic stateの失敗testを書く**

seed testは次を確認する。

- 三Sceneがsequence 1/2/3で入口だけactive。
- actorは全Sceneでpublic。
- goblinは奥の部屋だけpublicかつattack reachable。
- hallにperception、sanctumにpersuasionとstealth checkがある。
- runはruined_chapel version 1 active。
- 二回目のseedで進行済みScene、flag、ending、HP、inventoryを戻さない。

query/API testsはScenarioなしでadventure=None、Scenarioありで次のliteralをassertする。

    assert state.adventure.objective == "廃礼拝堂の奥から銀の聖印を回収する"
    assert state.adventure.current_scene.scene_ref == "entrance"
    assert [action.action_ref for action in state.adventure.available_actions] == [
        "enter_chapel"
    ]

- [ ] **Step 2: focused testsが失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_runtime.py tests/unit/test_turn_queries.py tests/integration/test_api.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "development_fixture or adventure_state"

Expected: FAIL because three-Scene fixture and adventure DTO do not exist。

- [ ] **Step 3: fixtureをScenario定義から冪等にseedする**

seed_development_fixtureはBUILTIN_SCENARIOSからScene、skill checks、runを作る。INSERT ON CONFLICT DO NOTHINGを使い、既存runとCanonical値をUPDATEしない。Entity label/refの既存保守更新は維持してよいが、HP、inventory、flags、Scene status、endingを上書きしない。

- [ ] **Step 4: 公開Adventure DTOを構築する**

TurnQueryServiceへScenarioCatalogを注入し、同じUnitOfWork内でturn stateとScenarioRunSnapshotを読む。Applicationのpublic context変換を使い、生flagや未公開条件を返さない。

    class CampaignStateResponse(Contract):
        state_version: NonNegativeInt
        latest_turn: TurnResponse | None
        adventure: AdventureState | None = None

create_appのdefault wiringはBUILTIN_SCENARIOSを使う。既存のmock service注入APIは変えない。

- [ ] **Step 5: CLI fixture出力を三Scene対応に更新する**

seed-fixtureのJSON出力へentrance_scene_id、hall_scene_id、sanctum_scene_idを追加し、campaign_id、principal_id、actor_id等の既存keyを維持する。

- [ ] **Step 6: focused testsを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_runtime.py tests/unit/test_turn_queries.py tests/integration/test_api.py
    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "development_fixture or adventure_state"

Expected: all selected tests passed。

- [ ] **Step 7: Task 7をコミットする**

    git add src/ai_rpg/runtime.py src/ai_rpg/contracts src/ai_rpg/application/turn_queries.py src/ai_rpg/api/app.py src/ai_rpg/cli.py tests/unit tests/integration/test_api.py tests/integration/postgres/test_migrations.py
    git commit -m "feat: expose ruined chapel adventure state"

---

### Task 8: 完走受入、文書、全体検証

**Files:**
- Modify: tests/integration/postgres/test_migrations.py
- Create: docs/adr/0012-scenario-definitions-and-progress.md
- Modify: docs/adr/README.md
- Modify: docs/ai-trpg-contracts-v0.2.md
- Modify: README.md

**Interfaces:**
- Verifies: fixture -> Turn accept -> resolution worker -> narration worker -> state query
- Documents: Scenario definition/state split、atomic progression、stage 1 limits

- [ ] **Step 1: 四つの完走受入testを書く**

共通helperは各Turnについて次を実行する。

1. current /stateからstate_versionを読む。
2. 新しいrequest_idでPlayerTurnInputを受付する。
3. resolution workerとnarration workerを一回ずつ実行する。
4. terminal Turnと次の/stateを読む。

四経路を別testにし、手書き期待値を使う。

- enter -> search success -> negotiate success -> recovered
- enter -> search failure -> negotiateまたはstealth -> costly_success
- enter -> search -> retreat -> retreated
- enter -> search -> attackを繰り返す -> goblin HP 0 -> recoveredまたはalerted時costly_success

各testはScene sequence、flag公開文、state_version、Action/Event件数、ending、完了後409を検証する。描写workerを同じTurnで再実行してもScenarioProgressed Eventとflagが増えないことも一経路で確認する。

- [ ] **Step 2: 受入testが未接続箇所を示して失敗することを確認する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "ruined_chapel"

Expected: 新しい完走testの少なくとも一つが、実際の接続不足を示してFAILする。既に全て通る場合はtestがproduction経路を本当に通っているか見直す。

- [ ] **Step 3: 受入testに必要な最小の接続修正だけを行う**

新しい抽象化や別Scenario機能を追加せず、fixture、worker wiring、public state、Fake文言の不一致だけを修正する。

- [ ] **Step 4: 受入testを通す**

Run:

    .\.venv\Scripts\python.exe -m pytest -q tests/integration/postgres/test_migrations.py -k "ruined_chapel"

Expected: all ruined_chapel tests passed。

- [ ] **Step 5: ADR、契約、READMEを更新する**

ADR-0012は次を決定として記録する。

- 定義はversion付き型付きJSON。
- run/flagsだけをDBへ保存。
- Scenario更新はMechanical commitに限定。
- Scene sequenceで定義SceneとCampaign Sceneを対応。
- downgradeでrun/flagを失い復元できない。

READMEにはseed-fixture、Fake worker、代表入力、三Ending、/state確認例を追加する。段階2の開始画面、実LLM、敵反撃、ブラウザログインが未実装であることを明記する。

- [ ] **Step 6: 文書整合を確認する**

Run:

    rg -n "ruined_chapel|ScenarioProgressed|ADVENTURE_COMPLETED|costly_success" README.md docs
    git diff --check

Expected: 実装契約と同じ用語が見つかり、whitespace errorなし。

- [ ] **Step 7: 全体testと静的検査を実行する**

Run:

    .\.venv\Scripts\python.exe -m pytest -q --basetemp .tmp-pytest-ruined-chapel\run -p no:cacheprovider
    node tests/browser/play_state.test.cjs
    node tests/browser/play_screen.test.cjs
    .\.venv\Scripts\python.exe -m ruff check .
    .\.venv\Scripts\python.exe -m mypy src
    .\.venv\Scripts\uv.exe lock --check
    .\.venv\Scripts\uv.exe build --offline
    git diff --check

PostgreSQL testではAIRPG_TEST_DATABASE_URLを専用DBへ設定する。Windows共有tempでAccess Deniedが出る場合はrepo内の専用basetempを使う。

- [ ] **Step 8: Task 8をコミットする**

    git add tests/integration/postgres/test_migrations.py docs/adr docs/ai-trpg-contracts-v0.2.md README.md
    git commit -m "docs: describe ruined chapel scenario"

- [ ] **Step 9: 最終差分をreviewする**

Baseはplan commit、HeadはTask 8 commitとし、Scenario定義、atomicity、fail-forward、既存Campaign互換性、実際の完走testを重点確認する。CriticalとImportantを修正後、全体検証を最終SHAでもう一度実行する。
