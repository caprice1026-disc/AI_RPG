# Architecture Decision Records

このディレクトリは、AI TRPG の実装を開始するために固定した意思決定を記録する。ADR の状態が「採用」の場合、本文中の古い「候補」「未決定」という記述よりADRを優先する。決定を変更するときは既存ADRを削除せず、新しいADRから置換対象を参照する。

| ADR | 判断 |
| --- | --- |
| [0001](0001-python-package-and-build.md) | Pythonパッケージ管理・ビルド方式 |
| [0002](0002-http-api-framework.md) | HTTP APIフレームワーク |
| [0003](0003-postgresql-access-and-migrations.md) | PostgreSQLアクセスとマイグレーション |
| [0004](0004-worker-runtime.md) | Turn／描写workerの実行基盤 |
| [0005](0005-streaming.md) | クライアント向けストリーミング |
| [0006](0006-llm-provider.md) | LLMプロバイダー抽象化（0013で置換） |
| [0007](0007-mvp-ruleset.md) | MVPゲームルール |
| [0008](0008-runtime-defaults.md) | 実行時初期設定 |
| [0009](0009-turn-routing.md) | Turnルーティング |
| [0010](0010-authenticated-principal.md) | 認証済みprincipal境界 |
| [0011](0011-scene-entity-scope.md) | Scene entityの公開範囲と攻撃到達可能性 |
| [0012](0012-scenario-definitions-and-progress.md) | Scenario定義と実行状態の分離 |
| [0013](0013-pydantic-ai-orchestration.md) | 用途別Pydantic AI Agentとモデル切替 |
| [0014](0014-registered-actions-and-enemy-reactions.md) | 登録済み行動の直接実行と敵の反撃 |
| [0015](0015-browser-oidc-sessions.md) | ブラウザOIDCとサーバー管理セッション |

## 共通方針

- 日付は決定日、状態は `提案`、`採用`、`廃止`、`置換` のいずれかとする。
- 技術固有型をDomain/Applicationへ漏らさず、交換境界をProtocolまたはportとして保つ。
- 秘密値は設定ファイルへ保存せず、実行環境のsecret storeから環境変数へ注入する。
