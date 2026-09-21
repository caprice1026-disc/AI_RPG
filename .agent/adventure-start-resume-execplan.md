# 段階2: 冒険の開始・再開・状態表示 ExecPlan

## 目的と範囲

シナリオとプリセットPCを選び、名前を入力して冒険を開始できるようにする。プレイヤーはUUIDの入力やDB操作を行わず、DBに保存された冒険一覧から再開する。現在地、目的、発見済み情報、HP、所持品、行動候補、過去の入力と描写を表示する。

段階1の進行・認可・冪等性を維持する。ブラウザログイン、実LLM調整、敵の反撃、自由なキャラクター作成、マルチプレイは対象外。既存HTML/CSS/JavaScriptを拡張し、依存やfrontend frameworkを追加しない。PLANS.mdは対象repoに存在しないため、既存.agent計画の形式を用いる。

## 設計

- 認証済みprincipalを使用し、開始payloadからprincipalやHPなどの正本値を受け取らない。subjectはidentity表のみ。
- 公開catalogは組込み短編ruined_chapel v1と少数の型付きPC presetを返す。隠し条件や未訪問Sceneの内容は返さない。
- 開始はCampaign、member、PC、NPC、装備、在庫、3 Scenes、技能判定、ScenarioRunを一transactionで作る。既存seed-devは互換性を維持し、新規開始で呼び出さない。
- principalとrequest_idをキーに開始要求を永続化する。同一payload再送は同じcampaign/actorを返し、異なるpayloadは409。並行要求、rollbackを実DBで検証する。
- 一覧は本人がactive memberで操作PCを持つ冒険のみ。公開状態・履歴も毎回認可する。Campaignロック下の短いtransactionで整合した状態を読み、秘密のflagsやNPC能力は公開しない。
- 履歴はDB由来の入力表示文と公開TurnResponseを時系列で表示する。上限付きページ取得で古い履歴へ遡れ、再読み込みで最新Turnも追跡できる。
- 開始／Turnとも通信結果不明時は同一request_idとpayloadをlocalStorageから再送する。未確認要求がある間は別の新規要求を作らない。古い非同期応答で別の冒険を上書きしない。
- 既存のdevelopment principalを明示的に有効化するAPI起動を維持し、引数省略時の固定開発principal指定を追加する。OIDCの既定認証を弱めず、公開ホストで使わない旨を説明する。

## Task 1: 開始・一覧・公開状態・履歴API

担当範囲: contracts/adventures.py、application/adventures.pyとport、infrastructure/postgres/adventures.py、models.py、migration 0011、api/app.py、cli.py、関連Python tests。既存型の拡張は互換defaultを付ける。

- [ ] 公開catalogとPC preset、開始request/response、一覧・player・履歴DTOを作る。
- [ ] 開始のatomic性・並行冪等性、認可、名前/preset validationをテストして実装する。
- [ ] GET /adventures/catalog、POST /adventures、GET /adventuresを追加する。
- [ ] GET /campaigns/{id}/stateに本人PCのactor_id/name/HP/inventoryを追加し、GET /campaigns/{id}/historyを追加する。
- [ ] 開始後のFake worker進行、履歴、他principal拒否、内部情報非公開を確認する。
- [ ] APIとレスポンスの確定例を次タスクへ渡す。実装reviewを行う。

## Task 2: 開始・再開のプレイ画面

担当範囲: api/static/index.html、play-state.js、必要なら分離した小さなJS、tests/browser/*。API契約はTask 1の実装に合わせる。

- [ ] シナリオ・preset・名前の開始フォームと、保存済み冒険一覧を追加する。UUID入力を通常導線から外す。
- [ ] 公開状態からHP/在庫/現在地/目的/発見/行動候補/結末を描画する。
- [ ] DB履歴の復元、古い履歴の取得、進行中Turnの追跡と完了後state再取得を接続する。
- [ ] 開始応答喪失、reload、Turn再送、古い応答、完了済み冒険、401をbrowser unit testで検証する。
- [ ] 既存の見た目とアクセシビリティを維持し、実装reviewを行う。

## Task 3: 実環境確認・文書・統合

- [x] Dockerの古いsocketを可逆的に退避し、既存PostgreSQLコンテナの復旧を確認する。
- [ ] README/API docsを更新する。開発環境だけで使う認証、Fakeの制約、開始・再開の操作を明記する。
- [ ] 専用test DBで全pytest、Ruff、mypy、JS tests、build、git diff --checkを実行する。
- [ ] test DBと分離した検証DBでAPI/Fake workersを起動し、desktop/mobileの実browserで開始→進行→reload→再開→結末を確認する。
- [ ] 最終review後、mainへfast-forwardしpush、remote SHAを照合する。

## 検証環境と記録

基点: main 49c2d739c8565984ace70a71275d33a1d07b8cee。
worktree: .worktrees/adventure-start-resume、branch: codex/adventure-start-resume。
共有Python: repo/.venv/Scripts/python.exe。実行時にPYTHONPATHをworktree/srcへ設定する。
テスト用PostgreSQL: localhost:55432/ai_rpg_test。統合testsはこの専用DBのみで実行する。

実施結果は各チェックと本節へ追記する。段階3以降を完了扱いにしない。

- 2026-09-22: Docker/runとdocker-secrets-engineの古いsocketを同時に退避してDocker Engine 29.8.0を復旧。既存ai-rpg-test-postgresを起動し、pg_isready成功、ai_rpg_testが空であることを確認。DB volume・設定・他のコンテナは変更していない。
- 実browser用にai_rpg_stage2_browserを新規作成。既存ai_rpg_browserのデータと統合test DBから分離する。
