# AI_RPG

[English](README.en.md) · [開発ガイド](docs/development.md) · [プレイテストガイド](docs/playtest-guide.md)

AI_RPGは、自由入力と行動候補で遊び、ブラウザで自分の短編を作れるAI TRPGのMVPです。
LLMが行動を提案し、確定した結果を描写します。判定・乱数・HP・所持品の変更はGame Engineが計算し、PostgreSQLへ保存します。

## Docker Composeで始める

Windows PowerShell、Docker Desktop、Gitを使います。プレイだけならPython・Node.jsのインストールは不要です。

```powershell
git clone https://github.com/caprice1026-disc/AI_RPG.git
Set-Location AI_RPG
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

既存の `.env` を保ったまま、`GEMINI_API_KEY` に自分のキーを設定します。既定モデルは `google:gemini-3.5-flash` で、実モデルの呼出には料金が発生します。キーをブラウザやGitへ入れないでください。
`AIRPG_LLM_MODEL` が共通モデル、`AIRPG_FAST_MODEL` が行動の解釈、`AIRPG_QUALITY_MODEL` が描写、`AIRPG_BACKGROUND_MODEL` が作品作成補助の上書き設定です。

```powershell
docker compose up --build -d
docker compose ps
```

ComposeはPostgreSQL、migration、API、解決・描写・作品作成補助の3つのworkerを起動します。
[http://127.0.0.1:8000/](http://127.0.0.1:8000/) を開いてください。ポートが使用中なら `.env` に `AIRPG_PORT=18769` などを設定し、起動後にそのポートを開きます。

このComposeはHTTPと固定の開発プレイヤーを使い、ポートをこのPCのloopbackだけに公開します。インターネットへ公開しないでください。APIキーなしのFake LLMと手動起動は[開発ガイド](docs/development.md#manual-local)を参照してください。

## 冒険を遊ぶ・作品を探す

組込み短編「廃礼拝堂の聖印」「霧灯台の航海日誌」、または「公開作品を探す」で選んだ作品を開始できます。公開作品は作品名・紹介文とタグで検索し、紹介や注意事項を確認してから遊べます。公開詳細の閲覧にログインは不要ですが、冒険開始にはログインが必要です。

冒険者名・プリセット・能力配分・得意技能を選ぶと、UUIDやDBの手入力なしで開始できます。行動候補を選ぶだけでなく、自由文で探索・交渉・工夫を試せます。失敗による警戒や時間経過も保存し、重大なリスクは確定前に確認します。HP、所持品、発見、過去のTurnを確認し、同じプレイヤーで冒険を再開できます。

画面と組込み短編は日本語です。Fake LLMが理解する自由文は一部の例に限られます。待機中は未確定の出目を表示せず、描写待ちでも確定済みのダイス結果だけを表示します。

## 自分の作品を作る

1. 「マイ作品」でテンプレートを選ぶか「空の原稿から作る」で始め、世界、場所、人物、アイテム、行動、条件、結末を編集します。テンプレートの「作成のヒント」で質問と推奨構成を確認できます。
2. 入力から2秒後に自動保存します。「保存済み」を確認してください。未完成の原稿も保存でき、競合時は入力を保持して保存済みの版と比較できます。履歴の復元と複製もできます。
3. 「原稿を検証」で形式・参照・登録行動の経路を確認し、エラー、警告、検証できた範囲を読みます。これはLLMを呼びません。
4. 「保存して最新原稿を試遊」で専用の冒険を開始します。試遊中に原稿を編集しても、その冒険の定義は変わりません。
5. 同じ内容を作者本人が少なくとも1つの結末まで試遊し、警告と公開内容を確認してから公開します。「自分のみ」「URLで限定共有」「一般公開」を選べます。限定共有は検索一覧に載りません。公開停止後も開始済みの冒険は開始時の版で続けられます。

AI作成補助はボタンを押したときだけ実行します。整合性チェック、不足部分の補完、条件からの構成案作成を依頼でき、構成案は編集・承認してから具体化します。再読み込み後も履歴から実行中の依頼に戻って進捗を確認したり、過去の結果を選んで見直したりできます。変更前後を比較して選んだ提案だけを採用します。自動保存はAIを呼ばず、AIも勝手に原稿へ反映したり公開したりしません。必要なサービスは `authoring-worker` です。

構造の検証は、自由入力の全経路、文章の意味、秘密の扱い、ゲームバランスや面白さを保証しません。公開時の作者確認は人が行うもので、自動テストやエージェントの試遊で代行したことにはしません。[保存・公開の仕様](docs/development.md#story-authoring)も参照してください。

## 停止・再開と運用上の制限

```powershell
docker compose down
docker compose up --build -d
docker compose logs --tail=80 api resolution-worker narration-worker authoring-worker
```

`down` はDBの名前付きvolumeを残します。`down -v` は冒険・原稿・公開版を削除するため、保存データを残す場合は実行しないでください。

OIDC参加登録は既定で無効（`AIRPG_REGISTRATION_ENABLED=false`）です。運用者が有効にした場合だけ、明示的な `/auth/login?join=true` から参加登録できます。通常のログインやBearer APIでは未登録identityを自動登録しません。参加者向けにはHTTPSと通常のOIDC認証を用意し、固定の開発プレイヤーを使わないでください。

日次・同時実行の上限は[設定例](.env.example)と[開発ガイド](docs/development.md#authoring-limits)を参照してください。人による作者操作・試遊の受入確認と、公開HTTPS環境の[Issue #19](https://github.com/caprice1026-disc/AI_RPG/issues/19)の受入条件は未解決です。20〜30分の所要時間、会話品質、楽しさも人の試遊で評価する対象です。

## 開発と資料

DB不要の確認はリポジトリのルートで実行します。

```powershell
uv sync --frozen
uv run --frozen pytest tests/unit tests/contract -q -p no:cacheprovider
uv run --frozen ruff check .
uv run --frozen mypy src
uv build
```

PostgreSQLテストには[専用の空テストDB](docs/development.md#test-database)を使います。DB未設定によるskipを成功扱いしません。画面を変更する場合はNode.js 22.14以上で、`frontend` 内の `npm.cmd ci`、`npm.cmd test`、`npm.cmd run typecheck`、`npm.cmd run build` を実行し、ソースと生成物を揃えます。

- [開発ガイド](docs/development.md) / [frontend README](frontend/README.md): 手動起動、設定、テスト、復旧。
- [ADR一覧](docs/adr/README.md) / [利用者が作る作品のADR](docs/adr/0017-user-authored-stories.md): 採用した設計判断。
- [プレイテストガイド](docs/playtest-guide.md) / [記録テンプレート](docs/playtest-record-template.md): 人の試遊と自動・エージェント検証の区別。
- [2026-09-29の検証記録](docs/verification-20260929.md): 当時の確認範囲。現在の変更の再検証を意味しません。

協力する場合は[Issues](https://github.com/caprice1026-disc/AI_RPG/issues)で既存の議論を確認し、[.AGENTS.md](.AGENTS.md)に従って変更理由・実行した検証・未確認の範囲をPull Requestに記載してください。
