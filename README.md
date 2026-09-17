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

現在のMVPでは、Fake LLMを使って次の一往復を実PostgreSQL上で再現できます。

```text
プレイヤー入力 → 意図抽出 → 技能判定／攻撃／回復 → atomic保存 → 結果描写 → SSE更新
```

> [!IMPORTANT]
> 現在は基盤機能を検証するMVPです。開発用のプレイ画面、攻撃・回復アイテム、SSEは利用できますが、本番認証adapterはまだ接続していません。

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

### 現在できること

- FastAPIによるTurn受付と、認可付きTurn取得
- Narrative／Mechanicalを分ける決定的なTurn Router
- `mvp_v1` rulesetによる再現可能な技能判定
- Fake transportによる通常描写とMechanicalへの昇格
- OpenAI Responses APIのStructured Outputs adapter（単発request・暗黙retryなし）
- 完了済みTurnだけから組み立てる、公開範囲を限定した複数Turn Context
- Campaign／Actorを指定してTurnを送信し、判定と描写を確認できる最小プレイ画面
- 登録済み参照だけを使う攻撃、HP下限／上限、回復ポーションと在庫消費
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

2026-09-17時点で、実PostgreSQLを指定した全スイートは`221 passed`です。

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

## Fake LLMで一往復を試す

実モデルのAPIキーは不要です。上記の専用PostgreSQLを用意し、次の受入テストを実行します。fixtureがmigration、Campaign／PC／Scene／技能条件、認証principal、固定ダイス、Fake応答を用意します。

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:PORT/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "fake_llm_skill_check_round_trip_reopens_turn_acceptance or narrative_route_commits_zero_actions_in_one_llm_call or narrative_escalation_reuses_first_call_as_mechanical_intent or narration_timeout_survives_worker_replacement_and_falls_back"
```

この受入テストは、次のシナリオを自動確認します。

1. `POST /campaigns/{campaign_id}/turns`で「周囲を注意深く観察する」を受け付ける。
2. 解決workerがDBで呼出予算を予約し、Fake Intentと固定出目10を使って`10 + 2 = 12`の成功を確定する。
3. 独立した描写workerが保存済み公開結果だけから文章とChoiceを保存する。
4. `GET /campaigns/{campaign_id}/turns/{turn_id}`と同一request再送が同じ公開DTOを返す。
5. 描写終端後に次のTurnを受け付ける。

通常のMechanical経路はFake呼出2回、Narrative通常応答は1回です。timeoutや不正出力も呼出予算を消費し、worker再生成後も予算とdeadlineをDBから引き継ぎます。

### APIとworkerを別processで動かす

実際のprocess分離を試す場合は、テストDBとは別に開発DBを用意します。

```powershell
docker run --rm --name ai-rpg-dev-postgres `
  -e POSTGRES_USER=airpg `
  -e POSTGRES_PASSWORD=airpg `
  -e POSTGRES_DB=airpg `
  -p 5432:5432 -d postgres:16
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost/airpg"
.\.venv\Scripts\ai-rpg.exe seed-dev
```

続いて3つのPowerShellを開き、同じ`AIRPG_DATABASE_URL`を設定して起動します。

```powershell
# Terminal 1: 開発principalを明示したAPI
.\.venv\Scripts\ai-rpg.exe api `
  --dev-principal 10000000-0000-0000-0000-000000000021

# Terminal 2: 解決worker
.\.venv\Scripts\ai-rpg.exe resolution-worker --fake

# Terminal 3: 描写worker
.\.venv\Scripts\ai-rpg.exe narration-worker --fake
```

Turnを投入し、返された`turn_id`をGETすると進行状態と描写を確認できます。

ブラウザでは`http://127.0.0.1:8000/`を開き、Campaign IDとActor IDを入力すれば同じ一往復を試せます。開発fixtureには`hero`、`goblin`、`iron_sword`、`healing_potion`が登録されているため、「周囲を注意深く観察する」「鉄の剣でゴブリンを攻撃する」「回復ポーションを飲む」を試せます。画面はSSEを優先し、接続できない場合はGET pollingで終端状態まで追跡します。送信中の二重送信を防ぎ、開発用principalを指定せずAPIを起動した場合は401の案内を表示して認証を暗黙に迂回しません。

```powershell
$requestId = [guid]::NewGuid()
$body = @{
  request_id = $requestId
  expected_state_version = 0
  actor_id = "10000000-0000-0000-0000-000000000031"
  content = @{ kind = "text"; text = "周囲を注意深く観察する" }
} | ConvertTo-Json -Depth 4
$turn = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/campaigns/10000000-0000-0000-0000-000000000001/turns" `
  -ContentType "application/json" -Body $body
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/campaigns/10000000-0000-0000-0000-000000000001/turns/$($turn.turn_id)"
```

`--once`を付けるとworkerは一回だけ取得を試みて終了します。processを停止・再起動しても、Turn、予算、lease、確定済み結果はPostgreSQLから引き継がれます。`--dev-principal`は開発時だけ明示的に有効化する認証差し替えで、通常起動では引き続き401を返します。

解決workerへ渡す直近の公開履歴は`AIRPG_RECENT_MESSAGES_LIMIT`で0〜100件に設定でき、既定値は20です。0にすると履歴を渡しません。対象は同じCampaign・Scene・Actorの終端Turnだけで、プレイヤー入力、公開Action結果、保存済み描写を古い順に渡します。

### OpenAI adapterを使う

Fakeの代わりに実providerを選ぶ場合だけAPI keyが必要です。`--fake`を外し、同じ環境変数を設定したworkerを起動します。

```powershell
$env:AIRPG_OPENAI_API_KEY = "..."
$env:AIRPG_FAST_MODEL = "gpt-5-mini"
$env:AIRPG_QUALITY_MODEL = "gpt-5.4"
.\.venv\Scripts\ai-rpg.exe resolution-worker
.\.venv\Scripts\ai-rpg.exe narration-worker
```

API keyは実provider選択時だけ検証され、Fake実行には不要です。各物理requestの直前にDBのTurn予算を予約し、timeout、拒否、不正JSON、Schema不一致、描写の事実逸脱も消費済みとして扱います。Mechanical描写は、保存済み結果にない数値、Canonical UUID、未登録の明示`@ref`を保存前に拒否します。

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
- [x] OpenAI Responses API adapter
- [ ] 本番認証adapter
- [x] 最小のプレイヤー向け画面
- [x] 攻撃・回復・アイテム使用のAPI経路
- [x] SSEによるリアルタイム更新

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
└── llm/              # Structured outputとFake transport

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
