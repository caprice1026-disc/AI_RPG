# 開発ガイド

[日本語README](../README.md) · [English README](../README.en.md) · [プレイテストガイド](playtest-guide.md)

ローカルで遊ぶための準備と3プロセスの起動はREADMEを参照してください。
この文書には、開発・検証で必要になるDB、API、シナリオ互換性、モデル設定、障害復旧の詳細をまとめています。
コマンドはWindows PowerShellで、指定がなければリポジトリのルートから実行します。

## 処理の境界と構成

LLMはプレイヤー入力から意図を提案し、保存済みの結果を描写します。
判定・乱数・HP・在庫の変更はGame Engineで計算し、Action、Event、Canonical State、Turnを同じDB transactionで確定します。旧冒険は`mvp_v1`、新規の自由行動型v3は`mvp_v2`を使用します。
Narrativeは通常会話、Mechanicalは判定や状態変更を伴う処理です。Turn Routerが経路を選び、必要ならNarrativeからMechanicalへ昇格します。

| 境界 | 責務 |
| --- | --- |
| LLM | `ActionIntent` の提案と確定結果の描写。Commandや状態変更を直接採用しない |
| Application | 認可、参照解決、所有・行動条件、Engine結果の検証、永続化projection |
| Engine | ダイス、難易度、補正、ダメージ、回復、アイテム消費 |
| PostgreSQL | atomic保存、同一 `request_id` の冪等受付、並行受付、commit応答喪失時の照会 |
| Worker | 解決・描写で独立したleaseとepoch、古いworkerによる保存の拒否 |
| 実行制限 | LLM予算、試行回数、deadlineをDBへ保存し、再起動後も維持 |
| 公開範囲 | Scene単位のEntity公開と攻撃到達可能性、Actorのprivate inventory |
| 認可 | 受付時、LLM呼出前、確定直前にCampaign membershipとActor操作権を再確認 |

```text
src/ai_rpg/
├── api/              # FastAPI、認証、公開endpoint、ビルド済みVue
├── application/      # Use case、認可、worker、projection
├── contracts/        # 外部入力、LLM入出力、公開DTO
├── domain/           # Command、Result、StateChange、Event
├── engine/           # 決定的なゲームルール
├── infrastructure/   # PostgreSQL行lock、保存前値照合、Repository
├── llm/              # Pydantic AI Agent、モデル構成、Fake LLM
└── scenarios/        # version付きScenario定義（固定v1・v2と自由行動型v3）
migrations/           # Alembic migration
frontend/             # Vue 3、TypeScript、Vite、画面の回帰テスト
tests/                # Unit、contract、PostgreSQL integration等
docs/                 # 設計、ADR、検証記録
```

設計の判断は[ADR一覧](adr/README.md)を参照してください。採用済みADRは、古い設計資料の候補・未決定の記述より優先します。

<a id="test-database"></a>
## 専用テストDBと検証

`AIRPG_DATABASE_URL` は冒険を保存する開発・実行用DBです。
`AIRPG_TEST_DATABASE_URL` はテスト専用の空DBで、名前を `ai_rpg_test` から始めます。
fixtureは名前と既存テーブルを確認し、条件に合わないDBを拒否します。テストで作成したテーブルやデータは後片付けの対象なので、遊ぶDBや共有・本番DBを指定しないでください。

Docker Desktopでは、開発用の5432番とは別の55433番にテストDBを起動できます。
この例もloopbackだけに公開します。同名コンテナがある場合は用途を確認してから、再開するか別名で作成してください。

```powershell
docker run --name ai-rpg-test-postgres `
  -e POSTGRES_PASSWORD=postgres `
  -e POSTGRES_DB=ai_rpg_test_local `
  -p 127.0.0.1:55433:5432 -d postgres:16
docker exec ai-rpg-test-postgres pg_isready -U postgres -d ai_rpg_test_local
```

`accepting connections` を確認してから、同じPowerShellで実行します。
テストDBへのmigrationはfixtureが担当するため、先に手動で `alembic upgrade head` を実行しません。

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:55433/ai_rpg_test_local"
uv run --frozen pytest -q -p no:cacheprovider
uv run --frozen ruff check .
uv run --frozen mypy src
uv build
```

PostgreSQL統合テストだけを実行する場合は、次を使います。

```powershell
uv run --frozen pytest tests/integration/postgres -q -p no:cacheprovider
```

DBの環境変数が未設定ならPostgreSQLテストはskipします。実行結果には成功・失敗・skipと対象環境を区別して記録してください。
テストは空DBからのmigrationに加え、既存revisionからheadへの更新、state version競合、古いlease、二重確定、rollbackも対象とします。

Windowsの共有一時領域でアクセス拒否が起きる場合は、実行ごとに新しい専用パスを指定します。
`--basetemp` はpytestが内容を消去するため、既存の作業フォルダーを指定しません。

```powershell
$airpgTestTemp = Join-Path $env:TEMP ("airpg-pytest-" + [guid]::NewGuid().ToString())
uv run --frozen pytest -q -p no:cacheprovider --basetemp $airpgTestTemp
```

検証後は `docker stop ai-rpg-test-postgres`、次回は `docker start ai-rpg-test-postgres` で停止・再開できます。
異常終了後に空DBチェックで拒否された場合は、残存内容を調べるか、新しい専用の空テストDBを用意します。

### Fake LLMの受入テスト

上のテストDBを指定したまま、APIキーなしで次を実行できます。
fixtureがmigration、Campaign、PC、Scene、技能条件、認証principal、固定ダイス、Fake応答を用意します。

```powershell
uv run --frozen pytest tests/integration/postgres/test_migrations.py -q -p no:cacheprovider -k "fake_llm_skill_check_round_trip_reopens_turn_acceptance or narrative_route_commits_zero_actions_in_one_llm_call or narrative_escalation_reuses_first_call_as_mechanical_intent or narration_timeout_survives_worker_replacement_and_falls_back"
uv run --frozen pytest tests/integration/postgres/test_migrations.py -q -p no:cacheprovider -k "ruined_chapel"
```

技能判定の一往復は、次の処理を確認します。

1. `POST /campaigns/{campaign_id}/turns` で「周囲を注意深く観察する」を受け付ける。
2. 解決workerがDBの呼出予算を予約し、Fake Intentと固定出目10から `10 + 2 = 12` の成功を確定する。
3. 描写workerが保存済みの公開結果だけから文章とChoiceを保存する。
4. TurnのGETと同一requestの再送が同じ公開DTOを返す。
5. 描写終端後に次のTurnを受け付ける。

通常のMechanical経路はFake呼出2回、Narrative通常応答は1回です。
timeoutや不正出力も予算を消費し、worker再生成後も予算とdeadlineをDBから引き継ぎます。
これらの自動テストは、実モデルの会話品質や人による試遊を評価するものではありません。

<a id="scenario-compatibility"></a>
## シナリオv3と旧v1・v2

ブラウザの新規冒険は短編v3です。開始時にプリセットを選び、`strength`・`agility`・`insight`・`presence`へ計2点を配分して、得意技能を1つ選びます。旧v1・v2の保存済み冒険は自動移行しません。

v3の自由文は、LLMが`open_action`として方法、必要なら能力・技能・難易度と成功/失敗の効果を提案します。Applicationが現在地、到達可能な主要地点、許可されたflag、結末条件、生成事実の参照を出目より前に検証します。生成事実は公開文・安定した`fact_ref`・作られた主要地点を保存し、再開後のContextと状態APIへ戻します。核心の場所・人物を新たに説明する文は生成事実として受け付けません。小さな場所は独立したSceneではなく、同じ主要地点内で参照できる事実です。AIの提案でHP・在庫・報酬を直接更新しません。

実行された行動は経過行動数を進めます。失敗時の警戒上昇などは保存され、確認質問・不正提案・重大リスクのキャンセルは時間を進めません。結末の確定、報酬を減らす代償、戦闘中の反撃によるHP喪失など、確定され得る効果から重大リスクを判定します。モデルが危険と表現しただけの通常判定は確認待ちにしません。確認対象の提案はDBに保存し、確認TurnではLLMを呼び直さずに権限と状態を再検証します。v3の公開状態は能力、警戒、経過行動数、結末の報酬説明を含みます。詳細は[ADR-0016](adr/0016-bounded-open-scenario.md)を参照してください。

登録済みの行動候補はv3でも現在のSceneと条件を確認して直接実行し、意図抽出LLMを呼びません。登録済みの攻撃は装備中の武器を使い、通常は結果描写の1回だけLLMを呼びます。再試行もDBの既存予算内で行います。

### 旧v1・v2の固定短編

次の表は保存済みv2の固定進行です。v3では同じ主要地点を使っても行動候補だけに限定せず、判定結果に応じた効果を確定します。

| Scene | 行動例 | 進行 |
| --- | --- | --- |
| 入口 | 依頼を引き受け、礼拝堂に入る | 広間へ進む |
| 広間 | 案内板と床を調べる | perception判定の成否にかかわらず記録庫へ進む |
| 記録庫 | 古い記録を読む | 手掛かり、または断片と代償を得て通路へ進む |
| 通路 | 帰り道を確保し、扉の向こうの声を聞く | 準備後に祭壇の部屋へ進む。聞き耳は任意 |
| 祭壇の部屋 | 交渉、隠密、ゴブリンへの攻撃 | 交渉・隠密は失敗しても代償付きで突破。戦闘開始後は攻撃か撤退 |
| 戦闘中 | 攻撃、回復ポーションを飲む | 適用された行動の後、生存する敵が1回反撃する |
| 帰還 | 聖印を回収し、村へ届ける | 冒険を完了する |

結末は `recovered`（回収成功）、`costly_success`（代償付き成功）、`retreated`（撤退）、`defeated`（敗北）です。
探索失敗でも先へ進めますが、回収の結末には代償が残ります。各所の撤退候補でも終了できます。

最初の攻撃は命中しなくても戦闘を開始します。生存する敵は、戦闘中の適用済みMechanical Turnの後に1回反撃します。
通常会話、未適用の行動、撤退、敵撃破によるScene離脱では反撃しません。PCのHPが0になると敗北で完了します。
反撃はプレイヤーActionと別に保存・表示し、描写を再試行しても追加攻撃しません。

全Sceneで `hero` を公開し、`goblin` は奥の部屋だけで公開・攻撃可能にします。
`iron_sword` と `healing_potion` はhero所有のprivate inventoryです。
v2の聖印回収は物語の進行状態として保存し、汎用inventory itemへは追加しません。

既存のv1冒険はv1のまま再開します。開発用の `seed-dev` もv1の三つのScene・三つの結末を維持します。
このコマンドは固定Campaign、主人公、ゴブリン、装備、技能条件、`ruined_chapel` runを冪等に作成し、IDをJSONで出力します。

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg seed-dev
```

`seed-dev` は通常のブラウザ開始には不要です。再実行しても、進行済みのScene、flag、Ending、HP、在庫を開始状態へ戻しません。

更新後はAPI・workerの起動前にmigrationを適用します。
`0012_registered_action_input` は旧text/choice入力を維持して登録行動の保存列を追加します。
登録行動Turnが残るDBのdowngradeは、データを変換・削除せず拒否します。
詳細は[ADR-0014](adr/0014-registered-actions-and-enemy-reactions.md)を参照してください。

<a id="http-sse"></a>
## HTTPとSSE

`GET /campaigns/{campaign_id}/state` は正本のstate versionと最新Turnを返します。
`adventure` には目的、現在Scene、公開済み事実、生成事実の参照と所属する主要地点、利用可能な登録済み行動、経過行動数、警戒、完了後のEndingと報酬説明が入ります。内部flag名や未達条件は返しません。
完了後の新規Turnは `409 ADVENTURE_COMPLETED` になります。

### 開発用HTTPリクエスト

READMEのAPIを `--dev-principal` で、両workerを `--fake` で起動しておきます。
次は別ターミナルから固定のv1 fixtureを作り、現在のstate versionで入力する例です。通常のブラウザ冒険には使いません。

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
$airpgFixture = uv run --frozen ai-rpg seed-dev | ConvertFrom-Json
$airpgCampaignUrl = "http://127.0.0.1:8000/campaigns/$($airpgFixture.campaign_id)"
$airpgState = Invoke-RestMethod -Uri "$airpgCampaignUrl/state"
$airpgRequestId = [guid]::NewGuid().ToString()
$airpgBody = @{
  request_id = $airpgRequestId
  expected_state_version = $airpgState.state_version
  actor_id = $airpgFixture.actor_id
  content = @{ kind = "text"; text = "礼拝堂に入る" }
} | ConvertTo-Json -Depth 4
$airpgTurn = Invoke-RestMethod -Method Post `
  -Uri "$airpgCampaignUrl/turns" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($airpgBody))
Invoke-RestMethod -Uri "$airpgCampaignUrl/turns/$($airpgTurn.turn_id)"
$airpgState = Invoke-RestMethod -Uri "$airpgCampaignUrl/state"
$airpgState.adventure | ConvertTo-Json -Depth 5
```

POSTは処理の受付です。最初のGETでは解決・描写が未完了の場合があるため、同じTurnをGETして終端を確認します。
fixtureが進行済みなら、現在のSceneで有効な入力へ変更してください。
POSTの結果が不明な場合は `$airpgRequestId` と `$airpgBody` を保持し、同じ本文だけを再送します。例全体を再実行すると別requestになります。

通常のBearer APIは `Authorization: Bearer <token>` が必要です。
この開発例の認証省略を公開APIへ適用しないでください。

### Turn更新の配信

`GET /campaigns/{campaign_id}/events` は認可済みCampaignの永続event sequenceをSSEの `id` に使います。
raw domain eventではなく、公開 `TurnResponse` へ投影した `turn.updated` を返します。
再接続時は `Last-Event-ID` を送り、そのsequenceより後を再取得します。

```text
id: 12
event: turn.updated
data: {"id":12,"type":"turn.updated","schema_version":1,"payload":{"turn":{...}}}
```

配信はat-least-onceなので、クライアントは `id` で重複を除外します。
サーバーは15秒ごとにheartbeatを送り、短いpollごとにCampaign membershipを再確認します。
書込みが詰まった接続はtimeoutで閉じ、workerやDB transactionを保持しません。ブラウザセッションの失効も配信時に確認します。

<a id="provider-settings"></a>
## Provider設定と呼出予算

アプリケーションはリポジトリ直下の `.env` と環境変数を読み込みます。環境変数が優先されます。
設定の正本は [config.py](../src/ai_rpg/config.py)、秘密値を含まない例は [.env.example](../.env.example)です。
キーはサーバーだけに置き、設定を変えたらworkerを再起動します。

| 設定 | 用途・優先順位 |
| --- | --- |
| `AIRPG_LLM_MODEL` | 共通モデル。既定値 `google:gemini-3.5-flash` |
| `AIRPG_FAST_MODEL` | 意図抽出・通常会話。未指定なら共通モデル |
| `AIRPG_QUALITY_MODEL` | 確定結果の描写。未指定なら共通モデル |
| `AIRPG_BACKGROUND_MODEL` | 将来のbackground用途。未指定なら共通モデル |
| `AIRPG_GEMINI_API_KEY` / `GEMINI_API_KEY` | Google API認証。両方を同じ設定元で指定した場合は左を優先 |
| `AIRPG_OPENAI_API_KEY` / `OPENAI_API_KEY` | OpenAI API認証。両方を同じ設定元で指定した場合は左を優先 |

tierのoverrideは共通モデルより優先します。実workerは全tierのproviderを起動時に準備するため、BACKGROUNDで指定したproviderもAPIキーが必要です。
Googleだけを使うならGoogleのキーだけで起動でき、複数providerを指定すればそれぞれのキーが必要です。Fakeではキーも外部LLM通信も不要です。

OpenAI Responsesへ切り替える場合は、既存の `.env` の該当項目を編集します。
以下は設定例で、モデルの利用可否は利用するアカウントで確認してください。既存のFAST／QUALITY／BACKGROUND overrideも合わせて確認します。

```dotenv
AIRPG_LLM_MODEL=openai-responses:gpt-5-mini
OPENAI_API_KEY=YOUR_API_KEY
```

旧形式の `gpt-5-mini` や `openai:gpt-5-mini` もOpenAI Responsesとして扱います。
Google/OpenAI以外の追加には対応extra、モデルfactory、Schema・例外・再送回数の適合試験が必要です。

### Agentと確定結果の検証

会話・意図抽出・結果描写の3用途のAgentが、Pydantic契約を `NativeOutput` として使います。
出力は型付きの `result` オブジェクトで包み、provider向けSchema変換はPydantic AIが担当します。旧手書きOpenAI HTTP transportは使用しません。
Agentにゲーム更新toolを渡さず、未検証の途中出力をSSEへ配信しません。

各物理requestの直前にworkerがDBのTurn予算を1回予約します。
Agentは `retries=0` と `UsageLimits(request_limit=1)`、Google SDKは `attempts=1`、OpenAI SDKは `max_retries=0` に固定します。
SDKによる自動repair・通信retry・provider fallbackは行わず、既存workerが残予算とdeadlineを確認して明示的に再試行します。
OpenAIは `store=False` とし、会話履歴はDBから組み立てます。

timeout、拒否、不正JSON、Schema不一致、描写の事実逸脱も予約済み予算を消費します。
Mechanical描写はプレイヤー入力を確定値の根拠にせず、保存済みEngine結果と認可済み公開状態にない数値、Canonical UUID、未登録の明示 `@ref` を保存前に拒否します。
この検査は自然言語の意味全体を保証するものではありません。

### 実行制限と履歴

| 設定 | 既定値 | 範囲・制約 |
| --- | --- | --- |
| `AIRPG_MAX_ACTIONS_PER_TURN` | 3 | 1〜10。受付時にTurnへ保存 |
| `AIRPG_WORKER_LEASE_SECONDS` | 60秒 | 30〜300秒 |
| `AIRPG_LLM_TIMEOUT_SECONDS` | 30秒 | 5〜120秒、leaseより短い値 |
| `AIRPG_RESOLUTION_MAX_ATTEMPTS` / `AIRPG_NARRATION_MAX_ATTEMPTS` | 各3回 | 1〜10 |
| `AIRPG_RESOLUTION_DEADLINE_SECONDS` / `AIRPG_NARRATION_DEADLINE_SECONDS` | 各120秒 | 60〜900秒、lease以上 |
| `AIRPG_NARRATIVE_CALL_BUDGET` | 1回 | 1固定 |
| `AIRPG_MECHANICAL_CALL_BUDGET` | 3回 | 3固定 |
| `AIRPG_RECENT_MESSAGES_LIMIT` | 20件 | 0〜100、0なら履歴なし |

最近の履歴は同じCampaign・Scene・Actorの終端Turnだけから組み立て、プレイヤー入力、公開Action結果、保存済み描写を古い順に渡します。
プレイヤー入力はuntrusted、Action結果とGM履歴はderived dataとして扱い、履歴中の命令をworkerへの指示にしません。
NarrativeからMechanicalへ昇格した最初の呼出も、Turn全体の予算に数えます。
詳細は[実行時設定](adr/0008-runtime-defaults.md)と[Pydantic AIのADR](adr/0013-pydantic-ai-orchestration.md)を参照してください。

## OIDCと認証障害

ブラウザの公開環境設定・参加者への案内・Keycloakのローカル確認は[プレイテストガイド](playtest-guide.md)を参照してください。
`AIRPG_DATABASE_URL` を明示して最新migrationを適用してから、identity登録とAPI起動を行います。
`principal_identities` を含むschemaが必要です。

Bearer APIでは単一の `AIRPG_AUTH_ISSUER`、必須の `AIRPG_AUTH_AUDIENCE`、非対称署名方式のallowlistを使います。
事前登録したIdP subjectを、内部の安定した `principal_id` に対応付けます。
次はHTTPSのIdPを使う管理用の例です。値は利用するIdPに合わせて置き換えます。

```powershell
$env:AIRPG_AUTH_ISSUER = "https://idp.example.com/"
$env:AIRPG_AUTH_AUDIENCE = "ai-rpg-api"
$env:AIRPG_AUTH_ALLOWED_ALGORITHMS = "RS256"
uv run --frozen ai-rpg auth register `
  --subject "oidc-subject-from-provider" `
  --principal-id "00000000-0000-0000-0000-000000000021"
uv run --frozen ai-rpg api --host 127.0.0.1 --port 8000
```

identityを無効化する場合は別ターミナルで同じDB・Issuerを設定し、`uv run --frozen ai-rpg auth disable --subject "oidc-subject-from-provider"` を実行します。
以後、同じsubjectのtokenでも認証されません。現状の管理CLIは付け替え・削除・再有効化を提供しません。

JWTのrole、email、name claimはCampaign認可に使わず、`auth_context`、event、ゲームデータへコピーしません。
membershipとActor権限はtoken検証後もDBの最新状態で判定します。
subjectは `principal_identities` の対応キーとして保存し、明示的な管理CLIのJSON出力を除き、ログ・event・ゲームデータ・HTTPエラーへ出力しません。

| HTTP status | 認証での意味 |
| --- | --- |
| `401` | tokenなし、不正・期限切れtoken、未登録・無効identity。理由を区別せず `UNAUTHENTICATED` を返す |
| `403` | 認証は有効だが、現在のCampaign membershipまたはActor操作権がない |
| `503` | 起動後のJWKS接続障害やidentity対応表のDB障害で認証を完了できない |

Discoveryはprocess起動時に取得し、失敗するとAPIを起動しません。
JWKSは300秒cacheし、未知の `kid` では直近取得から30秒のcooldown後に再取得します。
必要な取得に接続できない場合は503、既知鍵での署名不一致や更新後も鍵が選べないtokenは401です。
通常起動は認証設定を必須とし、`--dev-principal` へ自動的に切り替わりません。

ブラウザログインにはAuthorization Code Flow、PKCE、state、nonce検証を使います。
8時間のサーバーセッションを発行し、本番CookieはHttpOnly／Secure／SameSite=Laxです。
Cookie認証の更新要求はOriginとCSRF tokenを検証し、provider tokenはブラウザstorageへ保存しません。
ログアウト・identity無効化・期限切れをSSEでも検知します。公開時はHTTPSを使い、proxyにもcallback query、Authorization、Cookieを記録させない設定が必要です。

<a id="failure-recovery"></a>
## 障害復旧とクライアントの再送

API、解決worker、描写workerは独立したprocessです。
workerの `--once` は一回だけ取得を試みて終了します。通常は常駐させ、各processを `Ctrl+C` で停止します。
再起動時はTurn、予算、lease、確定結果をDBから引き継ぎ、設定変更で既存Turnの予算やdeadlineをリセットしません。

| 状況 | 確認・復旧 |
| --- | --- |
| 接続できない | 対象DBの `pg_isready` と、API・両workerが同じDB URLを使うことを確認する |
| Turnが処理中のまま | 解決・描写worker両方の稼働とDB接続を確認する。期限切れleaseはworkerが回収する |
| 描写timeout、不正出力 | 残予算と試行・deadlineの範囲で再試行し、尽きた場合はfallbackで終端化する |
| ゲーム確定後の描写失敗 | 保存済み結果を保持し、描写だけを回収する。判定・ダメージ・反撃は再適用しない |
| 古いstate versionによる409 | 最新stateを読み直し、操作を再確認する。古い行動を新versionで自動実行しない |
| 完了した冒険 | 結末と履歴を読むか、新しい冒険を開始する |
| セッション失効 | 同じプレイヤーで再ログインし、保存済み冒険や保留中の要求を再取得する |

ブラウザはstate APIで正本のversionと最新Turnを取得し、Campaign切替、別タブ更新、reload後に同期します。
Turn ID発行前の確定的な4xx入力エラーではpendingを消し、修正後に新しいrequest IDを発行します。
通信断、不明なPOST結果、5xx、Turn ID発行後の失敗では、同じrequest IDとbodyを保持します。

受付済みの `turn_id` が分かっている場合はPOSTせず、GET／SSEで追跡を再開します。
POSTの結果が不明な場合だけ、元と同じ内容を明示的に再送します。
SSEが失敗・timeoutした場合は接続を閉じてGET pollingへ移り、古いCampaignや過去の追跡処理から届いた結果を表示しません。
pollingは追跡試行ごとに最大30回で止まり、画面から結果の再取得を選べます。

Vueのpending requestは送信前にprincipal別のブラウザstorageへ保存します。
Campaign切替、ログアウト、画面の破棄時は追跡を止め、セッション失効時は非公開データを隠して再認証用のpendingを保持します。
描写の終端後に次の入力を送る設計と、再送・回収の境界は[frontend README](../frontend/README.md)にも記載しています。

## 過去の検証記録と未確認の範囲

以下はREADMEから移した過去の記録で、今回の文書整理による新しい実行結果ではありません。

2026-09-22の実Gemini検証では短編v2を13Turnの自由入力で完走し、会話、依頼受諾、探索、攻撃失敗と敵の命中、回復後の反撃、撃破、回収・帰還を確認したと記録されています。
25物理要求とDB予約25回が一致し、全Turnでfallbackなし、同一入力の再送・worker再実行による追加呼出は0回でした。
回復上限を踏まえた描写の修正後の3要求と、検証スクリプト修正前の1要求を含む累計は29要求です。
OpenAI側の当時の確認範囲は、実SDKとHTTP mockまでです。

段階5・Vue移行の3冒険・26Turn・30物理要求、認証、画面、テストの結果は[2026-09-22〜23の検証記録](verification-stage5-20260922.md)を参照してください。
この過去の検証を、以降の変更の再検証、人の少人数試遊、公開HTTPS環境の受入検証として扱いません。
20〜30分の所要時間、会話品質、楽しさは人の試遊で確認し、[記録テンプレート](playtest-record-template.md)で自動テスト・エージェント検証と区別します。

文書やテストから参加する場合のラベルはREADMEのContributingを参照してください。
GitHubの運用参考: [Community Profile](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories) / [good first issueの活用](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/encouraging-helpful-contributions-to-your-project-with-labels)。
