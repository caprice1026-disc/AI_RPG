# ADR-0001: uvとpyproject.tomlによるPythonパッケージ管理・ビルド

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

Python 3.11以上を対象とし、依存解決、仮想環境、lock、コマンド実行を **uv** に統一する。依存とツール設定の正本はルートの `pyproject.toml`、再現可能な解決結果はコミットする `uv.lock` とする。アプリケーションは `src` layoutの単一配布パッケージとし、PEP 517 build backendには **hatchling** を使う。CIは `uv sync --frozen` でlockとの差分を許さず、wheelを `uv build` で生成する。

## 採用理由

- 開発、CI、コンテナで依存解決と実行コマンドを一本化できる。
- `pyproject.toml` とlockの役割が明確で、手編集したrequirementsの同期漏れを避けられる。
- hatchlingはアプリケーションコードにビルドツール固有APIを持ち込まず、標準wheelを生成できる。

## 却下した案

- **pip + requirements.txt**: lock、開発用依存、パッケージメタデータを複数ファイルで同期する運用を避ける。
- **Poetry**: 有力だが、依存管理以外の独自ワークフローを増やす利点がMVPでは小さい。
- **setuptools**: 成熟しているが、拡張ビルドを必要としない本プロジェクトには設定面が過剰である。

## 後から交換できる境界

成果物を標準wheel、メタデータを `pyproject.toml` に限定する。uvやhatchlingの呼び出しは開発スクリプトとCIだけに置き、Applicationコードから参照しない。別のPEP 517 frontend/backendへの移行時も、Pythonパッケージのimport pathと公開APIを維持する。

## 影響

依存更新時は `uv.lock` も同じ変更で更新する。サービスのコンテナにはuv自体を残さず、ビルド済みwheelをインストールして起動できるようにする。
