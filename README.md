# AI_RPG

LLMが物語上の意図と描写を担当し、ゲーム上の判定・状態変更・履歴は決定的なEngineとPostgreSQLで管理するAI TRPG基盤です。

## 現在の状態

まだ実モデルやUIを接続した完成アプリではありません。現在はFake LLMで、入力から技能判定、確定、描写、次Turn受付までを実PostgreSQL上で一往復できます。

- FastAPIのhealth、Turn受付、認可付きTurn取得endpoint
- 認証済みprincipalのApplication境界と、未設定時に必ず401となる既定認証
- 受付時、LLM呼出前、確定直前のCampaign membership／Actor操作権再検証
- 型付きのPlayer／LLM／Command／Result／Event契約
- 再現可能なダイスと`mvp_v1`技能判定
- 難易度名からDCへの変換を含む、Applicationから独立した`mvp_v1` ruleset
- ADR-0009に基づく決定的なNarrative／Mechanical Router
- PostgreSQL migration、Turnの冪等受付、lease、atomicなAction／Event確定
- Infrastructure限定のSQLAlchemy型付きmodelと通常CRUD／snapshotの型付きクエリ
- Turn単位のDB永続LLM予算、phase別attempt／固定deadline
- 解決とは独立した描写lease／epochと、描写・Choice・GMイベントのatomic保存
- timeout、Schema不正、worker交代、予算・deadline到達時のDB回収とfallback
- 受付時state versionをworkerまで固定し、更新後の状態で黙って再判定しない競合処理
- lock待ちを含めたDB実時刻によるlease／deadline検証
- 描写がcompleted／fallbackになるまで次Turnを閉じるCampaign単位制約
- damage、healing、item consumptionの型付きStateChangeと保存projection
- Fake transportを使う技能判定、Narrative通常応答、Mechanical昇格
- 並行受付、stale worker、commit応答喪失、二重確定、transaction rollbackの実PostgreSQLテスト

実モデル接続、攻撃・回復・アイテム使用の実行経路、SSE、UIはまだ行いません。既定のHTTP認証は意図的に未設定で、テストだけが認証済みprincipalを注入します。

## 責務の境界

- `contracts/`: 外部入力、LLM入出力、公開レスポンス
- `domain/`: Engineが正本として扱うCommand、Result、StateChange、Event
- `engine/`: ダイス、HP、回復、在庫などのルール計算
- `application/`: Engine結果とAction／Event／Canonical mutationの対応検証・投影
- `infrastructure/`: PostgreSQL行lock、保存前値照合、SQL実行、外部adapter

LLM出力は`ActionIntent`であり、DBへ直接保存するCommandや状態変更ではありません。Engineが`DamageApplied`、`HealingApplied`、`ItemConsumed`を生成し、Applicationが一度だけ永続化単位へ変換します。

## 開発環境

Python 3.11以上とPostgreSQLを使用します。Windowsでは既存の`.venv`を利用できます。

```powershell
.\.venv\Scripts\uv.exe sync --frozen
.\.venv\Scripts\uv.exe run --frozen pytest -q -p no:cacheprovider
.\.venv\Scripts\uv.exe run --frozen ruff check .
.\.venv\Scripts\uv.exe run --frozen mypy src
.\.venv\Scripts\uv.exe build
```

2026-09-17時点で、実PostgreSQLを指定した全スイートは`171 passed`、Ruffとmypyも成功しています。

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

## Fake LLM一往復を自動受入テストで再現する

実モデルのAPIキーは不要です。上記の専用PostgreSQLを用意し、次の受入テストを実行します。fixtureがmigration、Campaign／PC／Scene／技能条件、認証principal、固定ダイス、Fake応答を用意します。

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:PORT/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "fake_llm_skill_check_round_trip_reopens_turn_acceptance or narrative_route_commits_zero_actions_in_one_llm_call or narrative_escalation_reuses_first_call_as_mechanical_intent or narration_timeout_survives_worker_replacement_and_falls_back"
```

中心シナリオは次を自動確認します。

1. `POST /campaigns/{campaign_id}/turns`で「周囲を注意深く観察する」を受け付ける。
2. 解決workerがDBで呼出予算を予約し、Fake Intentと固定出目10を使って`10 + 2 = 12`の成功を確定する。
3. 独立した描写workerが保存済み公開結果だけから文章とChoiceを保存する。
4. `GET /campaigns/{campaign_id}/turns/{turn_id}`と同一request再送が同じ公開DTOを返す。
5. 描写終端後に次のTurnを受け付ける。

通常のMechanical経路はFake呼出2回、Narrative通常応答は1回です。timeoutや不正出力も送信直前に予約した回数を消費し、worker再生成後も予算とdeadlineはDBから引き継がれます。
登録済み条件へ解決できない行動や行動不能なActorはprovider障害として再試行せず、1回のIntent取得後に`not_applied`として説明付きで終端化します。

この再現方法ではpytest harnessが認証principal、ASGI API、解決worker、描写workerを接続し、worker交代も別インスタンスで検証します。独立したAPI／workerプロセスを起動するfixture投入CLI、本番認証adapter、常駐worker runnerの配布は後続範囲です。

## 設計資料

- [Architecture](docs/ai-trpg-architecture-v0.1.md)
- [Contracts](docs/ai-trpg-contracts-v0.2.md)
- [ADR index](docs/adr/README.md)
- [MVP ruleset](docs/adr/0007-mvp-ruleset.md)
