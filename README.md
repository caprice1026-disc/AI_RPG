# AI_RPG

[English](README.en.md) · [開発ガイド](docs/development.md) · [プレイテストガイド](docs/playtest-guide.md)

AI_RPGは、自由入力と行動候補で冒険を進めるAI TRPGのMVPです。
LLMが入力の意図を読み取り、確定した結果を描写します。判定・乱数・HP・所持品の変更はGame Engineが計算し、アプリケーションがPostgreSQLへ保存します。

## 何ができるか

- ブラウザで短編「廃礼拝堂の聖印」を選び、プリセットに能力ポイント2点を配分し、得意技能を決めて開始する。
- 主要地点の間で自由な方法を試し、別経路や小さな場所・人物・手掛かりを見つける。判定の失敗も警戒や経過行動数として残り、再挑戦や別の方法を選べる。
- 探索、交渉、隠密、攻撃、回復、敵の反撃を通じて、回収成功・代償付き成功・別の解決・撤退・敗北の結末へ進む。目標を放棄したり早期に達成したりしてもよい。
- HP、所持品、能力、目的、発見済み情報、警戒、経過行動数、敵HPを保存済みの状態から確認する。重大な結末リスクは確定前に確認する。
- 同じプレイヤーで保存済みの冒険を再開し、過去の入力・描写・結末を読み返す。
- OIDCアカウントをアプリに事前登録した参加者で試遊し、任意の感想をテキストファイルへ保存する。

画面とシナリオは日本語です。Fake LLMならAPIキーなしで行動候補を試せます。
自由な会話や言い換えを使う冒険にはGeminiなどの実モデルを利用します。

現在は短編のMVPで、20〜30分の所要時間、会話品質、楽しさは人による試遊の評価対象です。
自動テストとエージェントによる動作確認は、[直近の検証記録](docs/verification-20260928.md)で区別しています。

## Docker Composeでローカルプレイ

Windows PowerShellとDocker Desktop、Gitがあれば起動できます。Python・Node.jsのインストールはプレイだけなら不要です。ComposeはPostgreSQL、API、解決worker、描写workerを起動します。

```powershell
git clone https://github.com/caprice1026-disc/AI_RPG.git
Set-Location AI_RPG
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

`.env` の `GEMINI_API_KEY` に自分のキーを設定します。既存の `.env` は上書きしません。モデルは既定で `google:gemini-3.5-flash` です。キーをブラウザやGitへ入れないでください。別のproviderへ切り替えるときは、同じファイルの `AIRPG_LLM_MODEL` と必要なproviderキーを設定します。用途別モデルは `AIRPG_FAST_MODEL`（入力の解釈）と `AIRPG_QUALITY_MODEL`（描写）で上書きできます。

```powershell
docker compose up --build -d
docker compose ps
```

[http://127.0.0.1:8000/](http://127.0.0.1:8000/) を開き、冒険者名・プリセット・能力配分・得意技能を選んで開始します。UUIDやDBの手入力は不要です。行動候補は道案内で、自由文でも地域内の探索や工夫を試せます。礼拝堂の外へ出る選択は結末になりますが、宇宙など物理的に不可能な移動は確定しません。長椅子を調べても勝手に次の部屋へ移動せず、移動は明示したときに確定します。

処理中はダイスの待機演出が出ますが、確定前の出目は見せません。HP・持ち物・発見済み情報は保存状態から表示します。ポーションは所持品の行から使用できます。重大なリスクの確認を「今回は見送る」にしても行動は実行されず、保存済みの提案は状況が変わるまで再表示できます。

停止・再開・更新は次のとおりです。`down` はDBの名前付きvolumeを残します。`down -v` は保存済み冒険を消すため、実行しないでください。

```powershell
docker compose down
docker compose up --build -d
docker compose logs --tail=80 api resolution-worker narration-worker
```

HTTPと固定の開発principalを使う構成なので、このComposeをインターネットへ公開しないでください。APIの公開ポートはこのPCの `127.0.0.1:8000` に限定しています。起動後に `api` がhealthyにならない場合は `docker compose ps` と上記ログを確認します。実モデルの呼び出しには利用料金が発生します。Fake LLMや手動の開発手順は[開発ガイド](docs/development.md)を参照してください。

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
