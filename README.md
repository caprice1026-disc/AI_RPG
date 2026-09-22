<div align="center">

# AI_RPG

**LLMは物語を語る。ゲームの真実は、EngineとPostgreSQLが守る。**

LLMの創造性と、決定的なゲームルール・永続状態・障害復旧を分離したAI TRPG基盤です。

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Status](https://img.shields.io/badge/status-MVP%20in%20progress-F59E0B)

[特徴](#特徴) ・ [クイックスタート](#クイックスタート) ・ [アーキテクチャ](#アーキテクチャ) ・ [コントリビューション](#コントリビューション)

</div>

## AI_RPGとは

AI_RPGは、LLMにゲーム状態を直接変更させないAI TRPGバックエンドです。LLMはプレイヤー入力から意図を抽出し、確定済み結果を描写します。判定、乱数、HPや在庫の変更、イベント履歴は、型付きのGame EngineとPostgreSQL transactionが担当します。

現在のMVPでは、ブラウザでシナリオとプリセットPCを選び、固定短編「廃礼拝堂の聖印」を遊べます。Geminiとの自由入力、確実に実行できる行動候補、敵の反撃を含む戦闘に対応しています。回収成功・代償付き成功・撤退・敗北の結末があり、保存済みの冒険一覧から続きを再開できます。

```text
プレイヤー入力 → 意図抽出 → 技能判定／攻撃／Scenario進行 → atomic保存 → 結果描写 → 状態表示
```

> [!IMPORTANT]
> 現在は基盤機能を検証するMVPです。本番APIはOIDC Bearer JWT認証に対応していますが、ブラウザログインは未実装です。bundled play screenは引き続き開発用principalで利用します。

## 特徴

| 領域 | AI_RPGが守ること |
| --- | --- |
| LLM境界 | LLM出力を`ActionIntent`として扱い、Commandや状態変更へ直接採用しない |
| Game Engine | ダイス、難易度、補正、damage、healing、item consumptionを決定的に計算する |
| 永続化 | Action、Event、Canonical State、Turnを一つのtransactionで確定する |
| 冪等性 | 同じ`request_id`の再送、並行受付、commit応答喪失でも二重適用しない |
| Worker | 解決と描写で独立したlease／epochを持ち、stale workerの保存を拒否する |
| 予算と復旧 | LLM呼出回数、試行回数、deadlineをDBに残し、再起動後も引き継ぐ |
| セキュリティ | 受付時、LLM呼出前、確定直前にCampaign membershipとActor操作権を再確認する |
| Scene scope | 公開Entityと粗い攻撃到達可能性をSceneごとに固定し、Actorの所持品はprivateに保つ |

### 現在できること

- FastAPIによるTurn受付と、認可付きTurn取得
- Narrative／Mechanicalを分ける決定的なTurn Router
- `mvp_v1` rulesetによる再現可能な技能判定
- Fake LLMによる通常描写とMechanicalへの昇格
- Pydantic AIによる会話・意図抽出・結果描写の3用途のAgent（Gemini／OpenAI切替、暗黙retryなし）
- 完了済みTurnだけから組み立てる、公開範囲を限定した複数Turn Context
- UUID入力が不要な冒険の開始、保存済み冒険の再開、DB由来のHP・所持品・履歴表示
- 開始要求とTurnの送信結果が不明な場合も、同じrequestを再送して追跡するプレイ画面
- 登録済み参照だけを使う攻撃、HP下限／上限、回復ポーションと在庫消費
- version付きScenario定義と、探索の成否からScene／公開情報／Endingへ進む固定短編
- 登録済み行動の直接実行（意図抽出LLMを呼ばない）、敵HP・戦闘状態の表示
- プレイヤー行動と区別した敵の反撃、回復後の反撃、勝利・敗北・撤退後の進行
- Campaign event sequenceをcursorにしたSSEと、接続失敗時のGET polling fallback
- PostgreSQL migrationとSQLAlchemy 2の型付きmodel／query
- timeout、Schema不正、worker交代、予算切れ、deadline到達時の回収とfallback
- state version競合、古いlease、二重確定、rollbackを含む実PostgreSQLテスト

## アーキテクチャ

```mermaid
flowchart LR
    P[Player input] --> API[FastAPI]
    API --> R[Deterministic Router]
    R --> I[LLM: Intent only]
    I --> A[Application validation]
    A --> E[Game Engine]
    E --> DB[(PostgreSQL)]
    DB --> N[LLM: Narration only]
    N --> DTO[Public Turn DTO]
    DB --> SSE[SSE: Public events]
    SSE --> P
```

- `contracts/`: 外部入力、LLM入出力、公開レスポンス
- `domain/`: Engineが正本として扱うCommand、Result、StateChange、Event
- `engine/`: ダイス、HP、回復、在庫などのルール計算
- `application/`: 認可、参照解決、Engine結果の検証、永続化projection
- `infrastructure/`: PostgreSQL行lock、保存前値照合、Repository、外部adapter

詳しい判断理由は[Architecture Decision Records](docs/adr/README.md)に残しています。

## クイックスタート

必要なものはPython 3.11以上、[uv](https://docs.astral.sh/uv/)、PostgreSQLです。

```powershell
.\.venv\Scripts\uv.exe sync --frozen
.\.venv\Scripts\uv.exe run --frozen pytest -q -p no:cacheprovider
.\.venv\Scripts\uv.exe run --frozen ruff check .
.\.venv\Scripts\uv.exe run --frozen mypy src
.\.venv\Scripts\uv.exe build
```

変更時は下記の専用DBで全スイートを再実行してください。

PostgreSQL統合テストには、名前が`ai_rpg_test`で始まる専用の空DBを指定します。fixtureは既存テーブルがあるDBを拒否します。
Alembicは空DBだけでなく、既存revisionからheadへの更新もテストします。

Docker Desktopを使う場合は、専用DBを次のように起動できます。

```powershell
docker run --rm --name ai-rpg-test-postgres `
  -e POSTGRES_PASSWORD=postgres `
  -e POSTGRES_DB=ai_rpg_test_local `
  -p 55433:5432 -d postgres:16
docker exec ai-rpg-test-postgres pg_isready -U postgres -d ai_rpg_test_local
```

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:55433/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres -q -p no:cacheprovider
docker stop ai-rpg-test-postgres
```

## 本番APIのOIDC認証

本番APIは、`AIRPG_AUTH_ISSUER`で指定した単一のOIDC Issuerが発行するBearer JWTを検証します。先に`AIRPG_DATABASE_URL`を設定し、最新migrationを適用してください。identity登録とAPI起動より前に`principal_identities`を含むschemaが必要です。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Issuer、必須Audience、許可する非対称署名algorithmを設定し、IdPのsubjectを内部の安定した`principal_id`へ事前登録してからAPIを起動します。

```powershell
$env:AIRPG_AUTH_ISSUER = "https://idp.example.com/"
$env:AIRPG_AUTH_AUDIENCE = "ai-rpg-api"
$env:AIRPG_AUTH_ALLOWED_ALGORITHMS = "RS256"

.\.venv\Scripts\ai-rpg.exe auth register `
  --subject "oidc-subject-from-provider" `
  --principal-id "00000000-0000-0000-0000-000000000021"

.\.venv\Scripts\ai-rpg.exe api --host 127.0.0.1 --port 8000
```

API requestは`Authorization: Bearer <token>` headerを付けます。JWTのrole、email、name claimはCampaign認可に使用せず、`auth_context`、event、ゲームデータへコピーしません。Campaign membershipとactor権限は、token検証後もPostgreSQLの最新状態で判定します。subjectはidentity対応キーとして`principal_identities`だけに保存し、明示的に実行した`auth register`／`auth disable`の管理CLI JSON以外では、ログ、event、ゲームデータ、HTTPエラーへ出力しません。

identityを無効化すると、以後のrequestは同じsubjectのtokenでも認証されません。このreleaseでは付け替え、削除、再有効化は行いません。

```powershell
.\.venv\Scripts\ai-rpg.exe auth disable `
  --subject "oidc-subject-from-provider"
```

HTTP statusの意味は次のとおりです。

- `401`: tokenなし、不正・期限切れtoken、未登録identity、無効identity。外部から理由を区別せず`UNAUTHENTICATED`を返します。
- `403`: tokenは有効ですが、現在のCampaign membershipまたはactor権限がありません。
- `503`: 起動後のJWKS接続障害、またはidentity対応表のDB障害により認証処理を完了できません。

Discoveryはprocess起動時に取得し、失敗した場合はAPIを起動しません。JWKSは300秒cacheし、未知の`kid`では直近取得から30秒のcooldown経過後に再取得してIssuer側の鍵rotationへ追随します。必要なJWKS取得に接続できない場合は503、既知鍵での署名不一致や更新後も鍵を選択できないtokenは、他の不正credentialと同じ401になります。

`ai-rpg api --dev-principal`はOIDCを迂回するローカル開発専用の明示的な起動方法です。UUIDを省略すると固定の開発principalを使い、`--dev-principal <UUID>`で別の開発principalも指定できます。通常のAPI起動やworkerへ暗黙適用されません。このモードでは接続者を同じプレイヤーとして扱うため、APIを外部公開しないでください。将来のブラウザログイン／server sessionは同じprincipal契約を生成する別adapterとして追加します。それまではbundled play screenからBearer loginはできず、ローカル開発では`--dev-principal`が必要です。

## Fake LLMで動作を試す

実モデルのAPIキーは不要です。上記の専用PostgreSQLを用意し、次の受入テストを実行します。fixtureがmigration、Campaign／PC／Scene／技能条件、認証principal、固定ダイス、Fake応答を用意します。

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:PORT/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "fake_llm_skill_check_round_trip_reopens_turn_acceptance or narrative_route_commits_zero_actions_in_one_llm_call or narrative_escalation_reuses_first_call_as_mechanical_intent or narration_timeout_survives_worker_replacement_and_falls_back"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "ruined_chapel"
```

この受入テストは、次のシナリオを自動確認します。

1. `POST /campaigns/{campaign_id}/turns`で「周囲を注意深く観察する」を受け付ける。
2. 解決workerがDBで呼出予算を予約し、Fake Intentと固定出目10を使って`10 + 2 = 12`の成功を確定する。
3. 独立した描写workerが保存済み公開結果だけから文章とChoiceを保存する。
4. `GET /campaigns/{campaign_id}/turns/{turn_id}`と同一request再送が同じ公開DTOを返す。
5. 描写終端後に次のTurnを受け付ける。

通常のMechanical経路はFake呼出2回、Narrative通常応答は1回です。timeoutや不正出力も呼出予算を消費し、worker再生成後も予算とdeadlineをDBから引き継ぎます。

### APIとworkerを別processで動かす

ブラウザで遊ぶ場合は、テストDBとは別に開発DBを用意します。次の作成コマンドは初回だけ実行してください。保存先のvolumeを残せば、コンテナ停止後も冒険を再開できます。

```powershell
docker run --name ai-rpg-dev-postgres `
  -e POSTGRES_USER=airpg `
  -e POSTGRES_PASSWORD=airpg `
  -e POSTGRES_DB=airpg `
  -v ai-rpg-dev-data:/var/lib/postgresql/data `
  -p 127.0.0.1:5432:5432 -d postgres:16
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost/airpg"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

続いて3つのPowerShellを開き、同じ`AIRPG_DATABASE_URL`を設定して起動します。

```powershell
# Terminal 1: 開発principalを明示したAPI
.\.venv\Scripts\ai-rpg.exe api --dev-principal

# Terminal 2: 解決worker
.\.venv\Scripts\ai-rpg.exe resolution-worker --fake

# Terminal 3: 描写worker
.\.venv\Scripts\ai-rpg.exe narration-worker --fake
```

`http://127.0.0.1:8000/`を開き、シナリオ、プリセットPC、名前を選んで「冒険を始める」を押します。CampaignやActorのUUIDを入力する必要はありません。行動候補を押すと、その登録済み行動を直接送信します。自由入力は文章を入力して「送信」を押します。現在地、目的、HP、所持品、発見済み情報、敵HPは、GMの文章から推測せず保存済みの状態を表示します。

ブラウザを閉じた後は、同じ開発principalで起動し、「保存済みの冒険」の一覧で冒険を選びます。入力と描写の履歴もDBから復元します。古い履歴は追加で読み込めます。完了した冒険は結末と履歴を読み返せ、新しい冒険は別の進行状態として開始します。

APIとworkerは各TerminalのCtrl+Cで停止できます。DBを停止する場合は`docker stop ai-rpg-dev-postgres`、再開する場合は`docker start ai-rpg-dev-postgres`を使います。DB volumeを削除すると保存済みの冒険も失われます。

既存の固定IDを使う開発・検証では、引き続き`ai-rpg seed-dev`を利用できます。このコマンドは固定Campaign、主人公、ゴブリン、装備、三つのScene、技能条件、`ruined_chapel` runを冪等に作成し、IDをJSONで表示します。再実行しても進行済みのScene、flag、Ending、HP、在庫は開始状態へ戻しません。通常のブラウザ開始には不要です。

新規冒険は短編v2で開始します。候補はLLMにラベルを再解釈させず、現在のSceneと条件を確認して実行します。登録済みの攻撃では装備中の武器を使います。通常は描写だけに1回のLLM呼出を使い、失敗時の再試行も既存のDB予算内に収めます。各Turnの描写完了後に次の入力を送ってください。

| Scene | 行動の例 | 処理 |
| --- | --- | --- |
| 入口 | 依頼を引き受け、礼拝堂に入る | 広間へ進む |
| 広間 | 案内板と床を調べる | perception判定後、成功・失敗のどちらでも記録庫へ進む |
| 記録庫 | 古い記録を読む | 手掛かり、または断片と代償を得て通路へ進む |
| 通路 | 帰り道を確保し、扉の向こうの声を聞く | 準備後に祭壇の部屋へ進む。聞き耳は任意 |
| 祭壇の部屋 | 交渉、隠密、ゴブリンへの攻撃 | 交渉・隠密は失敗しても代償付きで突破。戦闘開始後は攻撃か撤退 |
| 戦闘中 | 攻撃、回復ポーションを飲む | プレイヤー行動の後、生存する敵が1回反撃する |
| 帰還 | 聖印を回収し、村へ届ける | 冒険を完了する |

結末は`recovered`（回収成功）、`costly_success`（代償付き成功）、`retreated`（撤退）、`defeated`（敗北）の四つです。探索失敗でも先へ進めますが、回収の結末には代償が残ります。各所の撤退候補で冒険を終了できます。

敵は最初の攻撃（命中しなくても）で戦闘を開始します。戦闘中の適用済みMechanical Turnごとに、既存Engineで1回反撃します。通常会話、未適用の行動、撤退、敵撃破でSceneを離れるTurnでは反撃しません。PCのHPが0になると敗北で完了し、履歴を読み返すか、新しい冒険を開始できます。反撃はプレイヤーのActionとは別に保存・表示し、描写を再試行しても追加攻撃は起きません。

既存のv1冒険と`seed-dev`の三つのScene・三つの結末は変更せず、v1のまま再開します。v2の聖印回収は物語の進行状態として保存し、汎用inventory itemには追加していません。更新後はAPI・worker起動前に`alembic upgrade head`を実行してください。migration `0012_registered_action_input`は旧入力を維持して登録行動の保存列を追加します。登録行動Turnが残るDBのdowngradeはデータを変換・削除せず拒否します。

`GET /campaigns/{campaign_id}/state`の`adventure`には、目的、現在Scene、公開済み事実、利用可能な登録済み行動、完了後のEndingが入ります。生の内部flag名や未達条件は返しません。完了後の新規Turnは`409 ADVENTURE_COMPLETED`になります。

冒険では全Sceneで`hero`を公開し、`goblin`は奥の部屋だけで公開かつ攻撃到達可能にします。`iron_sword`と`healing_potion`はhero所有のprivate inventoryです。

段階4までの最小ループに対応しています。Fakeでは行動候補を使って完走でき、戦闘中の回復は「回復ポーションを飲む」と入力できます。Fakeは自由な会話や言い換えを理解する実モデルではありません。自由入力の冒険には下記の実モデルを利用してください。ブラウザログインと少人数のプレイテストは段階5として残しています。

画面は`GET /campaigns/{campaign_id}/state`から正本のstate versionと最新Turnを取得し、Campaign切替、別タブ更新、reload後に状態を同期します。409時は古い行動を新versionで自動実行せず、再確認を促します。turn ID発行前の確定的な4xx入力エラーではpendingを消し、修正後は新しいrequest IDで送信します。通信断・不明なPOST結果・5xx・turn ID発行後の失敗では、同じrequest IDとbodyを保持して同じ内容だけを再送します。受付済みの`turn_id`が分かっている場合はPOSTせずGET／SSE追跡を再開します。SSEが失敗またはtimeoutした場合はSSEを閉じてGET Pollingだけへ引き継ぎ、古いCampaignや世代の更新は表示しません。開発用principalを指定せずAPIを起動した場合は401を表示し、認証を暗黙に迂回しません。

依存なしのブラウザ状態ロジックはNode標準runnerでも確認できます。

```powershell
node tests\browser\play_state.test.cjs
node tests\browser\play_screen.test.cjs
```

```powershell
$requestId = [guid]::NewGuid()
$body = @{
  request_id = $requestId
  expected_state_version = 0
  actor_id = "10000000-0000-0000-0000-000000000031"
  content = @{ kind = "text"; text = "礼拝堂に入る" }
} | ConvertTo-Json -Depth 4
$turn = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/campaigns/10000000-0000-0000-0000-000000000001/turns" `
  -ContentType "application/json" -Body $body
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/campaigns/10000000-0000-0000-0000-000000000001/turns/$($turn.turn_id)"
$state = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/campaigns/10000000-0000-0000-0000-000000000001/state"
$state.adventure | ConvertTo-Json -Depth 5
```

`--once`を付けるとworkerは一回だけ取得を試みて終了します。processを停止・再起動しても、Turn、予算、lease、確定済み結果はPostgreSQLから引き継がれます。`--dev-principal`は開発時だけ明示的に有効化する認証差し替えです。通常起動ではbundled play screenからの未認証requestは引き続き401になりますが、事前登録済みidentityの有効なBearer requestは認証されます。

解決workerへ渡す直近の公開履歴は`AIRPG_RECENT_MESSAGES_LIMIT`で0〜100件に設定でき、既定値は20です。0にすると履歴を渡しません。対象は同じCampaign・Scene・Actorの終端Turnだけで、プレイヤー入力、公開Action結果、保存済み描写を古い順に渡します。プレイヤー入力はuntrusted、Action結果とGM履歴はderived dataであり、履歴中の命令はworkerへの指示になりません。

### Pydantic AIと実モデルを使う

AI処理はPydantic AIのAgentが担当します。既定モデルは`google:gemini-3.5-flash`です。リポジトリ直下の`.env`に`GEMINI_API_KEY`を設定し、`--fake`を外して二つのworkerを別ターミナルで起動します。[.env.example](.env.example)には秘密値を含まない設定例があります。実モデルの呼び出しには利用料金が発生します。

```powershell
$env:AIRPG_LLM_MODEL = "google:gemini-3.5-flash"
.\.venv\Scripts\ai-rpg.exe resolution-worker
.\.venv\Scripts\ai-rpg.exe narration-worker
```

| 設定 | 用途 |
| --- | --- |
| `AIRPG_LLM_MODEL` | 共通モデル。既定値は`google:gemini-3.5-flash` |
| `AIRPG_FAST_MODEL` | 意図抽出・通常会話。未指定なら共通モデル |
| `AIRPG_QUALITY_MODEL` | 確定結果の描写。未指定なら共通モデル |
| `AIRPG_BACKGROUND_MODEL` | 将来のbackground用途。未指定なら共通モデル |
| `AIRPG_GEMINI_API_KEY` / `GEMINI_API_KEY` | Google API認証。左の設定を優先 |
| `AIRPG_OPENAI_API_KEY` / `OPENAI_API_KEY` | OpenAI API認証。左の設定を優先 |

OpenAIへ切り替える場合は共通モデルを`openai-responses:gpt-5-mini`などに変更し、OpenAIのキーを設定します。tierのoverrideがある場合は、その設定が共通モデルより優先されます。旧形式の`gpt-5-mini`や`openai:gpt-5-mini`もOpenAI Responsesモデルとして扱います。選択したproviderのキーだけが必要で、Fakeではキーも通信も不要です。

3用途のAgentは既存のPydantic契約を`NativeOutput`へ渡します。出力は型付きの`result`オブジェクトで包み、provider向けSchema変換はPydantic AIが行います。手書きのOpenAI HTTP transportは廃止しました。Google/OpenAI以外の追加には、対応extraとモデルfactoryへの追加、Schema・例外・再送回数の適合試験が必要です。

workerは各Agent呼出前にDB予算を一度予約します。Agentの`retries=0`、`UsageLimits(request_limit=1)`に加え、Google SDKは`attempts=1`、OpenAI SDKは`max_retries=0`とし、自動repair・通信retry・provider fallbackを行いません。明示的な再試行は既存workerが残予算とdeadlineを確認して実施します。OpenAIの`store=False`を維持し、会話履歴はDBから組み立てます。Agentへゲーム更新toolは渡さず、未検証の途中出力もSSEへ流しません。

各物理requestの直前にDBのTurn予算を予約し、timeout、拒否、不正JSON、Schema不一致、描写の事実逸脱も消費済みとして扱います。Mechanical描写は、プレイヤー入力を確定値の根拠にせず、保存済みEngine結果と認可済み公開状態にない数値、Canonical UUID、未登録の明示`@ref`を保存前に拒否します。

2026-09-22の実Gemini検証では、短編v2を全13Turnの自由入力で完走しました。会話、依頼受諾、探索、攻撃失敗と敵の命中、回復後の反撃、撃破、回収・帰還を確認し、25物理要求とDB予約25回が一致しました。全Turnでfallbackはなく、同一入力の再送・worker再実行の追加呼出は0回でした。回復上限を踏まえた描写の修正後に3要求で再確認し、検証スクリプト修正前の1要求を含む累計は29要求です。OpenAI側は実SDKとHTTP mockでの検証までです。20〜30分のプレイ時間、会話品質、楽しさは実プレイヤーによる評価が必要です。

## SSEでTurn更新を受け取る

`GET /campaigns/{campaign_id}/events`は、認可済みCampaignの永続event sequenceをSSE `id`として、raw domain eventではなく公開`TurnResponse`へ投影した`turn.updated`を返します。再接続時は`Last-Event-ID`を送ると、そのsequenceより後を再取得できます。

```text
id: 12
event: turn.updated
data: {"id":12,"type":"turn.updated","schema_version":1,"payload":{"turn":{...}}}
```

配信はat-least-onceなのでClientは`id`で重複を除外します。serverは15秒ごとにheartbeatを送り、短いpollごとにCampaign membershipを再確認します。書込みが詰まった接続はtimeoutで閉じ、workerやDB transactionを保持しません。

## ロードマップ

- [x] Fake LLMによる技能判定の一往復
- [x] 冪等受付、atomic確定、独立した描写worker
- [x] 永続LLM予算、lease、deadline、障害復旧
- [x] 開発用の独立API／worker runner
- [x] 公開範囲を限定した複数Turn Context
- [x] Pydantic AIの用途別AgentとGemini／OpenAIのモデル切替
- [x] 本番認証adapter
- [x] 最小のプレイヤー向け画面
- [x] 攻撃・回復・アイテム使用のAPI経路
- [x] SSEによるリアルタイム更新
- [x] 固定短編v2の開始、探索、Scene遷移、回収・代償・撤退・敗北の結末
- [x] シナリオ選択を含む開始・再開画面
- [x] 実LLMの自由入力による短編の一通りの動作確認
- [x] 敵の反撃を含む最小戦闘ループ
- [ ] 20〜30分の所要時間・会話品質・遊びやすさのプレイテスト
- [ ] ブラウザログイン

## コントリビューション

AI_RPGは開発途中です。小さなドキュメント改善、テスト追加、境界の整理から参加できます。

1. [Issues](https://github.com/caprice1026-disc/AI_RPG/issues)で既存の議論を確認する。
2. 初参加なら[`good first issue`](https://github.com/caprice1026-disc/AI_RPG/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)から探す。
3. 大きな変更は実装前にIssueで目的と範囲を共有する。
4. Pull Requestには変更理由と実行したテストを書く。

Issueの目安として、次のラベルを使います。

| ラベル | 対象 |
| --- | --- |
| `good first issue` | 小さく独立し、初参加でも取り組みやすい変更 |
| `help wanted` | 設計・実装・検証への協力を募集する変更 |
| `documentation` | README、ADR、契約書、実行手順の改善 |

運用の参考: [GitHub Community Profile](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories) / [`good first issue`の活用](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/encouraging-helpful-contributions-to-your-project-with-labels)

## プロジェクト構成

```text
src/ai_rpg/
├── api/              # FastAPI endpoint
├── application/      # Use case、認可、worker、projection
├── contracts/        # 外部・LLM・公開DTOの型
├── domain/           # Command、Result、Event
├── engine/           # 決定的なゲームルール
├── infrastructure/   # PostgreSQLと外部adapter
├── llm/              # Pydantic AI Agent、モデル構成、Fake LLM
└── scenarios/        # version付き固定Scenario定義

migrations/           # Alembic migration
tests/                # Unit、contract、PostgreSQL integration
docs/                 # Architecture、ADR、実装方針
```

## 設計資料

- [Architecture](docs/ai-trpg-architecture-v0.1.md)
- [Contracts](docs/ai-trpg-contracts-v0.2.md)
- [ADR index](docs/adr/README.md)
- [MVP ruleset](docs/adr/0007-mvp-ruleset.md)
- [Fake LLM round trip](docs/ai-trpg-fake-llm.md)
