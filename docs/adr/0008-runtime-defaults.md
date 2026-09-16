# ADR-0008: MVPの実行時初期設定と設定元

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

運用開始時の上限を次の通り固定する。設定の型付き正本はInfrastructure層の単一 `Settings` model、値の供給元は環境変数とする。ローカル用 `.env` は使用してよいがcommitせず、本番ではdeploy manifest/secret storeから注入する。未指定時は表のdefaultを使い、範囲外または解釈不能な値ならfail fastで起動を止める。

| 設定 | 環境変数 | 初期値 | 許容範囲／扱い |
| --- | --- | ---: | --- |
| 1 TurnのAction上限 | `AIRPG_MAX_ACTIONS_PER_TURN` | 3 | 1〜10。受付時にTurnへsnapshot |
| worker lease期間 | `AIRPG_WORKER_LEASE_SECONDS` | 60秒 | 30〜300秒。20秒ごと（leaseの1/3以内）に更新 |
| 最近のメッセージ件数 | `AIRPG_RECENT_MESSAGES_LIMIT` | 20件 | 0〜100。新しい順に取得後、時系列順でLLMへ渡す |
| LLM 1リクエストtimeout | `AIRPG_LLM_TIMEOUT_SECONDS` | 30秒 | 5〜120秒。接続から応答完了までのwall-clock deadline |
| Narrative呼び出し上限 | `AIRPG_NARRATIVE_CALL_BUDGET` | 1回/Turn | MVPでは1固定 |
| Mechanical呼び出し上限 | `AIRPG_MECHANICAL_CALL_BUDGET` | 3回/Turn | MVPでは3固定 |
| 解決phase試行上限 | `AIRPG_RESOLUTION_MAX_ATTEMPTS` | 3回 | 1〜10。lease再取得を含む |
| 描写phase試行上限 | `AIRPG_NARRATION_MAX_ATTEMPTS` | 3回 | 1〜10。lease再取得を含む |
| 解決phase deadline | `AIRPG_RESOLUTION_DEADLINE_SECONDS` | 120秒 | 60〜900秒。初回取得時にDBへ固定 |
| 描写phase deadline | `AIRPG_NARRATION_DEADLINE_SECONDS` | 120秒 | 60〜900秒。初回取得時にDBへ固定 |

呼び出し回数はproviderへの**各物理requestを送信する直前**に永続的に1予約する。timeout、拒否、schema不正、provider切替、repairも消費し、SDK自動retryは無効にする。Mechanicalの通常内訳はIntent抽出1、確定結果の描写1、必要時のrepairまたは描写再試行1である。Narrativeが `ResolutionRequired` へ昇格した場合、そのNarrative requestをMechanicalのIntent抽出済み1回として数え、Turn全体の上限は3回とする。Directorは別job予算であり、このTurn予算を流用しない。

Action上限と呼び出し予算はTurn受付時にeffective値を保存し、deploy途中や再取得で変えない。lease、recent message件数、timeoutはprocess設定だが、実行logへeffective値を記録する。LLM timeout時は同じlease内で無制限に待たず、残予算とlease所有権を検査する。

phaseのattempt_count、初回開始時刻、deadline、next_attempt_at、内部failure_codeはTurnへ保存する。lease再取得でattempt_countは増えるがdeadlineは延長しない。LLM timeoutはworker leaseより短く、phase deadlineはworker lease以上でなければ設定検証で起動を止める。

## 採用理由

- コスト、待ち時間、worker引継ぎの初期挙動を実装者ごとの暗黙値にしない。
- 環境変数と型付きmodelに集約し、コンテナ環境で同じ検証を行える。
- Turnごとのsnapshotにより、設定変更とretryでAction数や課金上限が変わることを防ぐ。

## 却下した案

- **コード各所の定数**: 設定元とeffective値が追跡できず、変更漏れが起きる。
- **DBだけを動的設定元にする**: cache無効化と処理中Turnへの適用時点が複雑になる。
- **SDK retryを回数外にする**: 実際のprovider request数、費用、timeoutを制御できない。
- **全会話履歴を投入する**: context量と費用が無制限になり、古い未信頼入力が増える。

## 後から交換できる境界

Applicationへは検証済みの `RuntimePolicy` value objectを渡し、環境変数readerへ直接依存させない。将来DB/remote configへ移行しても同じfield、範囲、Turn snapshot規約を保つ。値の変更はdeploy設定で可能だが、MVP固定の呼び出し上限や許容範囲の変更は本ADRの置換としてレビューする。

## 影響

metricsにはeffective設定、予約済み/使用済み呼び出し数、timeout、lease更新失敗を含める。timeoutはHTTP全体のtimeoutではなく、個別provider requestのdeadlineである。
