# ADR-0013: 用途別Pydantic AI Agentと明示的なモデル切替

- 状態: 採用
- 決定日: 2026-09-22
- 置換対象: [ADR-0006](0006-llm-provider.md)

## 文脈と決定

手書きのOpenAI Responses HTTP transportをPydantic AIのAgentへ置き換える。初期の実モデルはGemini 3.5 Flashとし、GoogleとOpenAI Responsesの切替を設定で行う。Pydantic v2によるDTO検証と、Pydantic AIによるモデル実行は別の責務である。

Applicationには`NarrativeGenerator`、`IntentExtractor`、`ResultNarrator`を置く。入力は既存の公開Context、出力は既存の型付き判断または描写であり、SDKの型を公開しない。Agent間の受け渡しはworkerが順に行う。AgentにEngineやDB更新のtoolを渡さず、自律的なAgent間呼出やGraphは導入しない。

| 担当 | 責務 |
| --- | --- |
| Pydantic AI | 用途別指示、NativeOutput、provider向けSchema、型付き出力、例外分類 |
| Application / worker | 認可、参照・所有・行動条件、grounding、予算予約、lease、deadline、明示的retry |
| Engine | 判定、HP・在庫の変化、確定結果 |
| Infrastructure | Canonical・Action・Eventのatomic保存 |
| runtime | モデル選択、SDK clientの生成と同一event loop内での終了 |

保存済みTurnの`max_actions`から実際の出力型を生成する。既存の`make_decision_types()`はTypeAdapterを返し、新しい`make_decision_output_types()`はAgent用の型を返す。root Unionを受け付けないproviderにも対応できるよう、型付き`result`を持つobjectを出力に使う。独自のJSON Schema書換えは廃止する。

## 一回の予算予約と物理要求

workerがDBで所有epoch・有効lease・残予算を確認し、一回分を予約してcommitしてから用途別portを呼ぶ。AgentとFakeは予約しない。Agentは`retries=0`、`UsageLimits(request_limit=1)`、toolなしとする。Google SDKは`HttpRetryOptions(attempts=1)`、OpenAI SDKは`max_retries=0`を明示する。UsageLimitsだけではSDK内の再送を数えられないため、HTTP境界でも要求回数を検証する。

timeout、拒否、不正出力でも予約を返却しない。再試行は既存workerの残予算・attempt・deadlineに従い、providerの自動fallbackは使わない。描写失敗からEngineを再実行しない。型検証後も`validate_mechanical_narration()`で確定結果との対応を検証する。

## 設定と情報の扱い

`AIRPG_LLM_MODEL`は`google:gemini-3.5-flash`を既定値とし、既存のFAST／QUALITY／BACKGROUND overrideを維持する。GoogleとOpenAI Responses以外は起動時に拒否する。別providerの追加時はextra、client寿命、retry無効化、例外変換、実際のSchemaの適合試験を追加する。

OpenAIは`store=False`とし、provider側の会話継続を使わない。履歴は既存DBの公開範囲から組み立て、SSEは確定済みの公開Turn eventだけを配信する。SDKへ渡すキーはSecretStr設定から取り出し、raw response・秘密値は公開例外に含めない。外部telemetryは有効にしない。ローカル開発の`.env`はGit管理外、本番はsecret storeから注入する。

旧ADRのusage／request ID／latencyの永続記録は今回追加しない。呼出回数は既存DB列を維持し、その他の利用量監査は後続とする。ゲーム進行、敵反撃、UI、ブラウザログインはこの移行に含めない。

## 検証

Fake LLMは同じ用途別portと出力契約を使う。Agent内部はFunctionModel、provider境界は実SDKとHTTP mockで検証する。実PostgreSQLでは短編完走、描写再試行、DB予算、stale epoch、rollback、再送を確認する。実モデルの結果はFakeやmockの成功と区別して記録する。

参照: [Pydantic AI Output](https://pydantic.dev/docs/ai/core-concepts/output/)、[Retries](https://pydantic.dev/docs/ai/core-concepts/retries/)、[Testing](https://pydantic.dev/docs/ai/guides/testing/)
