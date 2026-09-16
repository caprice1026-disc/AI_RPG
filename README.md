# AI_RPG

LLMが物語上の意図と描写を担当し、ゲーム上の判定・状態変更・履歴は決定的なEngineとPostgreSQLで管理するAI TRPG基盤です。

## 現在の状態

まだプレイ可能な完成アプリではありません。現在は次の土台を実装しています。

- FastAPIのhealth endpoint
- 型付きのPlayer／LLM／Command／Result／Event契約
- 再現可能なダイスと`mvp_v1`技能判定
- PostgreSQL migration、Turnの冪等受付、lease、atomicなAction／Event確定
- damage、healing、item consumptionの型付きStateChangeと保存projection
- 並行受付、stale worker、二重確定、transaction rollbackの実PostgreSQLテスト

実装の次段階は、Infrastructure専用SQLAlchemy ORM modelへの通常CRUD移行、DB永続のLLM予算、描写専用lease／event、Fake LLMによる一往復E2Eです。

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

PostgreSQL統合テストには、名前が`ai_rpg_test`で始まる専用の空DBを指定します。fixtureは既存テーブルがあるDBを拒否します。

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@HOST:PORT/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres -q -p no:cacheprovider
```

## 設計資料

- [Architecture](docs/ai-trpg-architecture-v0.1.md)
- [Contracts](docs/ai-trpg-contracts-v0.2.md)
- [ADR index](docs/adr/README.md)
- [MVP ruleset](docs/adr/0007-mvp-ruleset.md)
