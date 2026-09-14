# Python基盤構築 ExecPlan

## 目的

ADRで決定済みのPython 3.11、Pydantic v2、uv、src layoutを採用し、各責務の依存方向が明確な実装開始点を作る。

## 実施内容

1. `pyproject.toml` とlock fileへ実行時・開発時依存およびツール設定を集約する。
2. Contracts、Domain、Application、Engine、Context、LLM、Infrastructure、APIの境界をパッケージとして作る。
3. DomainとEngineを外部SDKから独立させ、ApplicationのProtocolを介して外部adapterへ接続する。
4. Unit、Integration、Contractのテスト分類を用意し、静的検査とビルドを実行する。

## 検証

`uv run pytest tests/unit`、`uv run pytest`、`uv run ruff check .`、`uv run mypy src tests`、`uv build`を成功させる。
