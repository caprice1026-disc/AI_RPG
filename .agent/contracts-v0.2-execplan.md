# Contracts v0.2 分割実装 ExecPlan

## 目的

設計文書のPython契約を責務別モジュールへ分割し、LLM、Application、Domain、公開レスポンスの各境界で不正な入力を拒否できる実装にする。

## 実施内容

1. 共通の厳格型と不変な基底契約を定義し、プレイヤー入力、コンテキスト、LLM判断、レスポンスを分割する。
2. Applicationが生成するCommand、Engineの確定結果、永続化するEventをDomain配下へ分割する。
3. Turn受付時のAction上限をRepositoryへ明示的に渡し、同じ値からLLM判断用TypeAdapterを生成する。
4. 参照名とCanonical UUIDの対応表をApplication専用型として閉じ込める。
5. 正常・異常の組み合わせを網羅する契約テストを追加する。

## 検証

`uv run pytest`、`uv run ruff check .`、`uv run mypy src tests`、`uv build`を実行する。
