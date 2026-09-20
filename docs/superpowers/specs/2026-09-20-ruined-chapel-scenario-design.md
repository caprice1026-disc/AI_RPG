# 廃礼拝堂短編シナリオ設計

- 日付: 2026-09-20
- 対象: 段階1「固定短編の開始から結末まで」
- 状態: 承認済み設計

## 目的

Fake LLMと実PostgreSQLを使い、「廃礼拝堂から依頼品を回収する」固定短編を開始から結末まで完走できるようにする。

今回の成果物は、既存のTurn受付、Mechanical解決、Canonical State、worker lease、冪等性、描写再試行を維持したまま、次の一連の進行を追加する。

1. 入口から礼拝堂へ入る。
2. 広間を探索し、成功または失敗に応じた情報を得る。
3. 奥の部屋を交渉、隠密、戦闘、撤退のいずれかで処理する。
4. 回収成功、代償付き成功、撤退のいずれかを確定する。

失敗は進行停止にせず、次の判断または結末を生む。

## 設計原則

- シナリオ定義とプレイ中の状態を分離する。
- LLMは登録済み行動を提案するだけで、Scene、フラグ、結末を直接更新しない。
- Engineはダイス、攻撃、回復等のゲーム計算を担当し、シナリオ遷移はApplicationが判定する。
- Repositoryは型付き更新を同一transactionで保存し、シナリオ条件を再実装しない。
- 状態変更は解決workerで一度だけ確定し、描写workerの再試行では再適用しない。
- 既存のCampaign lock、state version、worker epoch、Action/Event履歴を維持する。
- 新しい依存、汎用workflow engine、シナリオ編集UIは追加しない。

## シナリオ定義

組込み定義を型付きJSONとして src/ai_rpg/scenarios/ruined_chapel.json に配置し、Pydanticで読み込む。

定義は次の情報を持つ。

- scenario_ref
- version
- title
- objective
- Sceneのscene_ref、sequence、タイトル、公開説明
- Sceneで利用できる登録済み行動
- 行動の前提条件
- 成功・失敗時の遷移先、追加フラグ、結末
- 結末のID、タイトル、公開説明

loaderは少なくとも次を拒否する。

- 重複したScenario、Scene、Action、Ending参照
- 重複または1未満のScene sequence
- 存在しないSceneまたはEndingを指す遷移
- サポート外の技能参照
- 完了効果とScene遷移を同時に指定した行動結果
- 初期Sceneがsequence 1でない定義

定義は実行時に変更しない。Campaignは開始時のscenario_refとversionを保存し、異なるversionの定義で継続しない。

## 永続化する進行状態

新しいmigrationで次の二表を追加する。

    mvp_scenario_runs(
      campaign_id PRIMARY KEY,
      scenario_ref,
      scenario_version,
      status,        -- active | completed
      ending_ref,    -- completed時だけ値を持つ
      FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
    )

    mvp_scenario_flags(
      campaign_id,
      flag_ref,
      PRIMARY KEY(campaign_id, flag_ref),
      FOREIGN KEY(campaign_id) REFERENCES mvp_scenario_runs(campaign_id)
    )

現在地は既存のscenes.statusを正本とする。Scenario定義のScene sequenceとscenes.sequenceを対応させ、追加のScene mapping表は作らない。

activeなScenario runでは同Campaignにactive Sceneが一つ存在する。完了時は現在Sceneをclosedにし、Scenario runをcompletedとしてending_refを保存する。完了後はactive Sceneを残さない。

一般的なbackfillは行わない。既存CampaignはScenario runを持たず、従来の一往復動作を維持する。downgradeはScenario runとflagを削除し、進行状態を復元できないことを文書化する。

## 登録済みシナリオ行動

既存のActionIntentへ次の型を追加する。

    ScenarioActionIntent(
      kind = "scenario_action",
      action_ref
    )

対応するDomain commandと保存済みAction kindもscenario_actionを追加する。これは自由なstate mutationではなく、現在Sceneの定義に登録された行動を参照する命令である。

scenario_actionはMechanical確定経路を使う。Narrative routeが状態変更を提案した場合は、既存のResolutionRequiredでMechanicalへ昇格させる。Narrative commitは引き続きCanonical Stateを変更しない。

Applicationは実行前に次を確認する。

- Scenario runがactiveである。
- TurnのSceneが現在のactive Sceneである。
- action_refが現在Sceneに登録されている。
- 必要な進行フラグを満たしている。
- 無効化条件となるフラグを持っていない。

条件不一致はnot_appliedとし、Scene、フラグ、結末を変更しない。

## シナリオ進行

### 入口

- 公開目的: 廃礼拝堂の奥から銀の聖印を回収する。
- 登録行動: enter_chapel
- 効果: 入口をclosed、広間をactiveにする。

### 広間

- 登録判定: search_hall
- 技能: perception
- 難易度: normal
- 成功: clue_foundを追加して奥の部屋へ進む。
- 失敗: alertedを追加して奥の部屋へ進む。

成功・失敗のどちらでも現在Sceneをclosed、奥の部屋をactiveにする。

### 奥の部屋

次の四経路を許可する。

1. negotiate_guard
   - persuasion判定。
   - 成功しalertedがなければrecovered。
   - 失敗またはalertedがあればcostly_success。
2. sneak_to_relic
   - stealth判定。
   - 成功しalertedがなければrecovered。
   - 失敗またはalertedがあればcostly_success。
3. ゴブリンへの既存attack
   - 対象HPが0になるまでは奥の部屋に残る。
   - HPが0になったAction確定時に、alertedがなければrecovered、あればcostly_success。
4. retreat
   - ダイスなしでretreatedを確定する。

結末は次の三つとする。

- recovered: 回収成功
- costly_success: 代償付き成功
- retreated: 撤退

## Application境界

Scenario定義を読む小さなcatalogと、進行条件を評価する純粋なApplication componentを追加する。RepositoryやEngineへScenario分岐を置かない。

Canonical snapshotには、存在する場合だけ次を含める。

- Scenario runの参照、version、status、ending
- active Sceneのsequence
- 獲得済みflag集合

既存CampaignではScenario情報をNoneまたは空集合とし、現在のworker動作を変えない。

ApplicationはAction解決結果から、最大一つの型付きScenarioProgressUpdateを作る。

    ScenarioProgressUpdate(
      from_scene_id,
      to_scene_id | None,
      add_flags,
      ending_ref | None
    )

複合Action内で二つ以上のScenario進行更新が必要になる計画は、最初のEngine/RNG呼出し前に拒否する。通常の攻撃や回復など、Scenario進行を伴わない複合Actionは既存どおり扱う。

## Atomic commitとEvent

CommitBundleへ任意のScenarioProgressUpdateを追加する。PostgreSQL RepositoryはCampaign lock下で次を一つのtransactionとして保存する。

1. ActionとAction Event
2. HP、在庫等のCanonical更新
3. Scenario flag追加
4. Scene status変更またはEnding確定
5. Scenario進行Event
6. Campaign state_version更新
7. Turn確定

Scenario進行Eventは一件のScenarioProgressedとし、from/to Scene、追加flag、endingを保存する。公開Event payloadには公開可能な参照だけを含める。

Scenarioだけが変化するscenario_actionでもstate versionを増やす。技能判定や攻撃と同時にScenarioが進む場合は、同じcommitで一度だけ増やす。

worker epoch、lease、deadline、base state version、現在Sceneが一致しない更新は既存の条件付きcommitと同様に拒否する。確定後のworker再実行は既存結果を返し、Sceneを再変更しない。

## LLM ContextとFake LLM

Scene Contextの固定文を、Scenario定義から得た次の公開情報へ置き換える。

- 現在Sceneのタイトルと公開説明
- 現在の目的
- 公開済みの手掛かり
- 現在登録されている行動参照と表示ラベル

遷移条件、未発見情報、別Sceneの説明、Ending条件はLLMへ渡さない。Scenario定義はtrusted dataだが、LLM出力には状態変更権限を与えない。

Development Fakeはプレイヤー文から次の決定的応答を返す。

- 入る: enter_chapel
- 調べる／探索: search_hall
- 交渉: negotiate_guard
- 隠れる／忍び寄る: sneak_to_relic
- 攻撃: 既存のゴブリン攻撃
- 撤退: retreat

ダイス結果は引き続き注入された乱数源が決める。Fake LLMは成功、失敗、HP、結末を決めない。

## 公開状態API

GET /campaigns/{campaign_id}/stateの互換性を保ちながら、任意のadventureを追加する。

    adventure(
      scenario_ref,
      title,
      objective,
      status,
      current_scene | None,
      discovered_facts,
      available_actions,
      ending | None
    )

公開状態には生の内部flag名や未達条件を含めない。flagとendingはScenario定義の公開文へ変換する。Scenario runを持たない既存Campaignではadventureをnullにする。

今回、一般利用者向けのCampaign作成APIは追加しない。既存の開発fixture投入を礼拝堂Scenario開始へ拡張し、すべてのScene、Entity、技能条件、runを冪等に作成する。再seedは既存runのstatus、flag、ending、HP、在庫を巻き戻さない。

完了済みrunへの新規Turnは409 ADVENTURE_COMPLETEDで拒否する。同じrequest_idによる既存Turnの再取得は、従来どおり新規受付判定より先に行う。

## エラーと回復

- 定義の構造不正や参照切れはloaderで拒否する。
- 未登録または条件不一致のScenario actionは進行を変更しない。
- DB更新途中の失敗はAction、Scene、flag、Event、Turnをすべてrollbackする。
- 描写timeoutまたは失敗では既存fallbackを使い、確定済み進行を戻さない。
- 描写再試行ではScenario更新を再適用しない。
- 終了済みScenarioの公開状態と履歴は読取可能に保つ。

## テスト戦略

### 定義とApplication

- 有効な礼拝堂JSONを型付き定義として読み込める。
- 重複参照、参照切れ、無効な技能、遷移とEndingの競合を拒否する。
- 入口、探索success/failure、交渉、隠密、戦闘、撤退を公開状態から評価できる。
- 未登録actionと条件不一致では進行更新を作らない。
- 複数のScenario進行を含む複合計画をRNG前に拒否する。

### PostgreSQL

- migration、ORM mapping、外部キー、status/ending整合性を検証する。
- flag追加、Scene切替、Ending、Event、Turn確定が同一transactionで保存される。
- Event INSERT失敗時にすべてrollbackする。
- stale worker epoch、期限切れlease、古いversion、Scene不一致を拒否する。
- 同じTurnの再実行でScene、flag、Eventが増えない。
- downgradeでScenario進行が失われ、upgradeでは復元されない。

### 完走受入

実PostgreSQL、Development Fake、固定乱数で次を開始から結末まで実行する。

- 探索成功から交渉成功し、recoveredへ到達する。
- 探索失敗後も奥へ進み、costly_successへ到達する。
- 奥の部屋でretreatし、retreatedへ到達する。
- ゴブリンを複数回攻撃してHP 0と同時にEndingへ到達する。

各Turn後に/state相当の公開DTOを確認し、現在Scene、公開情報、利用可能行動、EndingがDB状態と一致することを検証する。

### 全体検証

- Python unit、contract、integration、PostgreSQL integration
- Node browser回帰テスト
- Ruff、mypy
- lock整合性、offline package build
- migration upgrade、downgrade、再upgrade

## READMEと契約文書

- READMEへ礼拝堂Scenarioの開始方法、代表入力、完走経路、制約を追加する。
- docs/ai-trpg-contracts-v0.2.mdへScenario run、進行更新、公開状態を追加する。
- Scenario定義と実行状態を分離する判断を新しいADRへ記録する。
- ADR indexを更新する。

## 今回の対象外

- UUID入力を不要にする開始・再開画面
- 一般利用者向けCampaign／キャラクター作成API
- 実LLMの調整と評価
- 敵の反撃と敗北処理
- ブラウザOIDCログイン
- 複数Scenarioの管理画面
- シナリオ自動生成、Director、RAG、NPC Agent
- 報酬、成長、装備店、課金

## 却下した案

- Scenario条件をPostgreSQL triggerやRepositoryへ実装する: Applicationと同じルールが二重化する。
- 礼拝堂専用の条件分岐をworkerへ直書きする: 二本目のScenarioで再利用できず、定義と状態が混ざる。
- 汎用workflow engineを追加する: 一本の固定Scenarioには過剰である。
- Narrative commitから直接Sceneを変更する: 状態変更と描写再試行の境界が曖昧になる。
- 全Scenario定義をDB正規化する: 今回必要なauthoring機能に対してmigrationとRepositoryが増えすぎる。
- 既存Campaignへ礼拝堂Scenarioをbackfillする: 既存の一往復fixtureと利用中Campaignの意味を変更する。

## 完了条件

- 開発fixtureから礼拝堂Scenarioを開始できる。
- Fake LLMで入口、探索、奥の部屋、三種類のEndingを再現できる。
- 失敗した探索でも進行が止まらない。
- Scenario進行がAction確定とatomicで、worker／描写再試行で重複しない。
- /stateから再開に必要な公開Scenario状態を取得できる。
- 既存ScenarioなしCampaignのTurn処理とAPI互換性を維持する。
- 全体テスト、静的解析、lock確認、package buildが成功する。
