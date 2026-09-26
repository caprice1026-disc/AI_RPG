# AI_RPG

[English](README.en.md) · [開発ガイド](docs/development.md) · [プレイテストガイド](docs/playtest-guide.md)

AI_RPGは、自由入力と行動候補で冒険を進めるAI TRPGのMVPです。
LLMが入力の意図を読み取り、確定した結果を描写します。判定・乱数・HP・所持品の変更はGame Engineが計算し、アプリケーションがPostgreSQLへ保存します。

## 何ができるか

- ブラウザでシナリオと冒険者を選び、短編「廃礼拝堂の聖印」を開始する。
- 探索、交渉、隠密、攻撃、回復、敵の反撃を通じて、回収成功・代償付き成功・撤退・敗北の結末へ進む。
- HP、所持品、目的、発見済み情報、敵HPを保存済みの状態から確認する。
- 同じプレイヤーで保存済みの冒険を再開し、過去の入力・描写・結末を読み返す。
- OIDCアカウントをアプリに事前登録した参加者で試遊し、任意の感想をテキストファイルへ保存する。

画面とシナリオは日本語です。Fake LLMならAPIキーなしで行動候補を試せます。
自由な会話や言い換えを使う冒険にはGeminiなどの実モデルを利用します。

現在は短編のMVPで、20〜30分の所要時間、会話品質、楽しさは人による試遊の評価対象です。
自動テストとエージェントによる動作確認は、[直近の検証記録](docs/verification-20260926.md)で区別しています。

## 最短でローカルのFakeを遊ぶ

以下はWindows PowerShellの手順です。Git、Python 3.11以上、インストール済みの[uv](https://docs.astral.sh/uv/)、起動中のDocker Desktopを用意します。
既存のPostgreSQLを使う場合は、専用の空の開発DBを用意し、接続URLを読み替えてください。

Vueのビルド済み画面を同梱しているため、遊ぶだけならNode.jsは不要です。
この手順は自分のPC内だけで使います。固定の開発principal（プレイヤーID）を使うAPIや、HTTPの開発環境を外部公開しないでください。

### 1. 取得と依存関係の準備

既存のチェックアウトがある場合は、そのルートへ移動して `uv sync --frozen` から進めます。

```powershell
uv --version
git clone https://github.com/caprice1026-disc/AI_RPG.git
Set-Location AI_RPG
uv sync --frozen
if (-not (Test-Path -LiteralPath .env)) {
  Copy-Item -LiteralPath .env.example -Destination .env
}
```

`uv sync --frozen` がローカルの `.venv` を作成します。事前の仮想環境作成やactivateは不要です。
上のコピーは既存の `.env` を上書きしません。FakeではAPIキーを設定する必要はありません。

### 2. 開発DBを用意する

新しい開発環境で一度だけ実行します。冒険は名前付きvolume `ai-rpg-dev-data` に保存します。
同名のコンテナやvolumeをすでに使っている場合は、既存の用途を確認してから再利用するか、別名を指定してください。

```powershell
docker run --name ai-rpg-dev-postgres `
  -e POSTGRES_USER=airpg `
  -e POSTGRES_PASSWORD=airpg `
  -e POSTGRES_DB=airpg `
  -v ai-rpg-dev-data:/var/lib/postgresql/data `
  -p 127.0.0.1:5432:5432 -d postgres:16
docker exec ai-rpg-dev-postgres pg_isready -U airpg -d airpg
```

`pg_isready` が `accepting connections` を返してから、リポジトリのルートでmigrationを適用します。
DB起動直後に失敗した場合は、少し待って `pg_isready` を再実行してください。

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen alembic upgrade head
```

5432番ポートが使用中ならDockerのホスト側ポートと、以降すべての接続URLを同じ値へ変更します。
Alembicは `.env` を直接読まないため、migrationでも `AIRPG_DATABASE_URL` を明示します。
この開発DBをテスト用の `AIRPG_TEST_DATABASE_URL` に指定しないでください。

### 3. APIと2つのworkerを起動する

PowerShellを3つ開き、**それぞれ同じリポジトリのルートへ移動**して実行します。
各ターミナルの環境変数は共有されないため、同じDB URLを3つとも設定します。

ターミナル1 — API:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg api --dev-principal
```

ターミナル2 — 行動の解決:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg resolution-worker --fake
```

ターミナル3 — 結果の描写:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg narration-worker --fake
```

`--dev-principal` は接続者を同じ開発プレイヤーとして扱う、loopback限定の認証モードです。
通常のAPI起動には自動適用されません。別プレイヤーの開発確認には `--dev-principal <UUID>` を使えます。

### 4. 遊ぶ・停止する・再開する

[http://127.0.0.1:8000/](http://127.0.0.1:8000/) を開き、シナリオ、冒険者、名前を選んで「冒険を始める」を押します。
CampaignやActorのUUID入力、`seed-dev` の実行は不要です。まず行動候補を選び、描写完了後に次の入力を送ります。
Fakeの自由入力は限られた表現だけに対応し、戦闘中の回復には「回復ポーションを飲む」を使えます。

APIとworkerは各ターミナルの `Ctrl+C` で停止します。DBの停止・再開は次のコマンドを使います。

```powershell
docker stop ai-rpg-dev-postgres
# 再開するとき
docker start ai-rpg-dev-postgres
```

再開時はDBの準備完了を確認し、手順3の3プロセスを同じDB・同じprincipalで起動します。
ブラウザで保存済みの冒険を選ぶと続きを遊べます。完了した冒険の履歴も残ります。
コードを更新した場合は、APIとworkerを起動する前に手順2のmigrationを再実行してください。

送信結果が不明な場合は画面の「同じ要求を再送」を使います。
受付済みのTurnは結果の取得を再開します。詳しい挙動は[復旧手順](docs/development.md#failure-recovery)を参照してください。

## Geminiへ切り替える

Pydantic AIを経由し、既定モデルは `google:gemini-3.5-flash` です。実モデルの呼び出しには利用料金が発生します。
Fakeの2つのworkerを `Ctrl+C` で停止してから、リポジトリ直下の既存の `.env` で次の項目を編集します。
例をファイル全体へ上書きせず、APIキーの値は手元で設定してください。

```dotenv
AIRPG_LLM_MODEL=google:gemini-3.5-flash
GEMINI_API_KEY=YOUR_API_KEY
```

キーはサーバーの `.env` またはsecret設定だけに置き、Gitへcommitしません。
ブラウザへ入力したり、フロントエンドの `VITE_` 変数へコピーしたりしないでください。

同じルート・同じDB設定のターミナル2と3で、両方の `--fake` を外して再起動します。
APIはローカル用のまま利用できます。

ターミナル2:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg resolution-worker
```

ターミナル3:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg narration-worker
```

モデルは `AIRPG_LLM_MODEL` で共通設定し、用途別に切り替える場合だけ次を指定します。

| 設定 | 用途 |
| --- | --- |
| `AIRPG_FAST_MODEL` | 意図抽出・通常会話 |
| `AIRPG_QUALITY_MODEL` | 確定結果の描写 |
| `AIRPG_BACKGROUND_MODEL` | 将来のbackground用途（設定したproviderのキーは起動時に必要） |

未指定のtierは共通モデルを引き継ぎます。既存のtier設定は共通モデルより優先するため、切替時に確認してください。
環境変数は `.env` より優先し、設定変更はworker再起動後に反映されます。
OpenAI Responsesへの切替、キーの優先順位、呼出予算は[provider設定](docs/development.md#provider-settings)にまとめています。

## 参加者向けのOIDCログイン

参加者に公開する場合は、IdPで確認したidentityの事前登録とHTTPSを用意します。
[プレイテストガイド](docs/playtest-guide.md)にブラウザclient、redirect URI、登録・無効化、ログ設定、ローカルKeycloakの手順があります。
開発principalを参加者用の認証として公開しないでください。

ブラウザ用client IDとBearer APIのAudienceは別設定です。APIと両workerは同じDBへ接続します。
セッションは8時間で失効し、ログアウト・identity無効化でも利用できなくなります。
感想の保存は任意で、サーバーへの自動送信はありません。

## 開発とテスト

リポジトリのルートで、DB不要のテストと静的検査を実行できます。

```powershell
uv run --frozen pytest tests/unit tests/contract -q -p no:cacheprovider
uv run --frozen ruff check .
uv run --frozen mypy src
uv build
```

全スイートは[開発ガイドの専用テストDB手順](docs/development.md#test-database)で実行します。
DB未設定によるskipを、PostgreSQL統合テストの成功として扱わないでください。

画面を変更するときはNode.js 22.14以上を使い、次を実行します。

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
Set-Location ..
```

ソースと `src/ai_rpg/api/static/vue/` の生成物を一緒にcommitします。
ログイン・Cookie・SSEを含む確認には、FastAPIから配信した同じoriginの画面を使います。詳細は[frontend README](frontend/README.md)を参照してください。

## 資料

- [開発ガイド](docs/development.md): 構成、テストDB、HTTP/SSE、シナリオ互換性、provider設定、障害復旧。
- [Architecture](docs/ai-trpg-architecture-v0.1.md) / [Contracts](docs/ai-trpg-contracts-v0.2.md) / [ADR一覧](docs/adr/README.md): 設計と採用済みの判断。
- [MVPルール](docs/adr/0007-mvp-ruleset.md) / [Fake LLMの旧実装記録](docs/ai-trpg-fake-llm.md): ルールと開発の経緯。
- [プレイテストガイド](docs/playtest-guide.md) / [記録テンプレート](docs/playtest-record-template.md): 人による試遊とエージェント検証を分けて記録。
- [Vue画面の設計](docs/vue-design.md) / [直近の検証記録](docs/verification-20260926.md) / [2026-09-22〜23の記録](docs/verification-stage5-20260922.md): 画面設計、確認範囲と制約。

## Contributing

[Issues](https://github.com/caprice1026-disc/AI_RPG/issues)で既存の議論を確認し、大きな変更は目的と範囲を共有してください。
小さな変更は [good first issue](https://github.com/caprice1026-disc/AI_RPG/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)、協力募集は `help wanted`、文書改善は `documentation` が目安です。
作業時は[.AGENTS.md](.AGENTS.md)に従い、Pull Requestへ変更理由・実行した検証・未確認の範囲を記載してください。
