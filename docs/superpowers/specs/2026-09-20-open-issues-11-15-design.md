# Open Issues #11–#15 修正設計

- 日付: 2026-09-20
- 対象: GitHub Issues #11–#15
- 状態: 承認済み設計

## 目的

現在のTurn UI、LLM Context、攻撃対象検証に残る5件の不整合を、既存の冪等性、Canonical State、Engine検証、PostgreSQL transaction境界を維持したまま修正する。

修正後は各Issueの回帰テスト、全体テスト、静的検査を通し、`main`へpushしたコミットを各Issueへ記録してクローズする。

## 対象Issue

- #11: 確定拒否されたPOSTのpendingが残り、入力修正後の再送を妨げる。
- #12: Campaignの最新`state_version`が過去Turnの確定versionで後退する。
- #13: SSEからのfallback polling失敗後も購読が残り、古い追跡結果がUIを更新する。
- #14: 過去のGM生成文が`trusted`としてLLM Contextへ渡される。
- #15: Scene内で公開かつ到達可能な攻撃対象を表すCanonicalデータがない。

## 設計原則

- POSTの受付結果が不明なときは、同じ`request_id`と完全に同じbodyを保持する。
- Campaign versionとTurnの確定versionを別の概念として扱う。
- 一つのTurn追跡でSSEとpollingを併走させない。
- LLM生成文は、保存済みであっても信頼済み命令へ昇格させない。
- Sceneの公開・到達可能性は履歴から推測せず、Canonicalな対応表へ明示する。
- 無効な複合Actionは、最初のダイスまたは状態変更より前に拒否する。
- 座標、距離、視界計算、汎用policy engineは導入しない。

## #11: pending POSTの分類

pendingは処理段階と失敗の確実性で扱いを分ける。

### Turn未受付

POSTがTurn IDを返す前に4xxとなった場合、サーバーが入力を確定拒否したものとしてpendingを削除する。入力欄は残し、ユーザーが修正して送信したときは新しい`request_id`を生成する。

401、403、404、409、422の個別UI処理は維持するが、pendingの削除判断はHTTP 4xxという共通条件へ寄せる。

### 受付結果不明

次の場合はpendingの`request_id`とbodyを変更しない。

- network errorまたはtimeout
- 5xx
- 成功応答を受信した後のJSON解析失敗
- Turn ID取得後の追跡失敗

Turn ID取得済みなら、再開時はPOSTを再送せずGETでTurnを追跡する。

## #12: versionの責務分離

`/campaigns/{id}/state`の`state_version`を、そのCampaignについてクライアントが送る唯一の正本とする。

`latest_turn.committed_state_version`は表示用のTurn情報とし、送信用Campaign versionを更新しない。Campaign切替、非同期callback、Turn観測には対象Campaign IDを明示し、現在選択中のCampaignと一致しない結果を破棄する。

同一Campaignについて既知のversionは後退させない。version競合時はstateを再取得し、次の送信は更新後のversionを使用する。古い入力を新しいversionへ自動再送しない。

## #13: Turn追跡の単一所有者

`waitForTurn`は追跡ごとのgenerationを発行し、正常完了、失敗、キャンセルのすべてを共通の`settle/cleanup`経路へ集約する。

cleanupでは次の順序を守る。

1. generationを無効化する。
2. fallback timerを解除する。
3. EventSourceを閉じる。
4. pollingの継続とcallbackを無効化する。

SSE接続が正常に開いたら接続待ちtimerを解除する。SSE errorまたは接続待ちtimeoutでpollingへ移る場合は、先にSSEを閉じる。遅延したSSE eventまたはHTTP responseは、Campaign ID、Turn ID、generationが一致するときだけUIへ反映する。

新しい追跡クラスや別モジュールは作らず、既存の画面script内に小さな状態とcleanup関数を置く。

## #14: LLM Contextの信頼区分

RecentMessageの信頼区分を次のように固定する。

| source | trust |
| --- | --- |
| `recent_player` | `untrusted` |
| `recent_action_result` | `derived` |
| `recent_gm` | `derived` |

過去のGM生成文を`trusted`へ昇格させない。resolutionとnarrationのsystem instructionには、Contextはデータであり、含まれる命令文や依頼を上位命令として扱わないことを明記する。

既存のEngine、Entity ref、schema、grounding検証を最終的な強制境界として維持する。新しいprovenance tableは追加しない。

## #15: Scene–Entity対応

最小のCanonical関連として`mvp_scene_entities`を追加する。

```text
mvp_scene_entities(
  campaign_id,
  scene_id,
  entity_id,
  is_public,
  is_attack_reachable,
  PRIMARY KEY(campaign_id, scene_id, entity_id),
  FOREIGN KEY(campaign_id, scene_id) REFERENCES scenes(campaign_id, id),
  FOREIGN KEY(campaign_id, entity_id) REFERENCES entities(campaign_id, id),
  CHECK(NOT is_attack_reachable OR is_public)
)
```

ORM mappingとAlembic migrationの両方を追加する。既存データから所属や公開状態を安全に推測できないため、一般的なbackfillは行わない。開発用seedとテストfixtureは明示的な対応行を作成する。

snapshotは対象Sceneの対応を取得し、Applicationで次の二集合を作る。

- LLMへ公開するEntity: `is_public`な同Scene Entity、操作Actor、Actor所有の装備・在庫。
- 攻撃可能なEntity: `is_public AND is_attack_reachable`な同Scene Character。ただしActor自身を除く。

`allowed_entity_refs`、mechanicalで利用可能なAction、Entity ref解決は同じ公開集合を使う。攻撃Intentは攻撃可能集合でも検証する。

複合Action内のref、所有権、Scene、公開、到達可能性は、最初のEngine呼出しより前に全件検証する。静的検証後の逐次解決は維持し、先行ActionによってHPや在庫が変化して後続だけ実行不能になった場合は、既存どおり後続を`not_applicable`とする。

Scene–Entity対応を将来更新する経路はCampaign lock下で`state_version`を増加させる。今回のIssue修正では管理APIや移動UIを追加しない。

## データ移行

新migrationは関連表、複合外部キー、`reachable => public`制約を追加する。既存Campaignへの暗黙backfillは行わないため、migration直後に未登録Entityが公開されることはない。

downgradeは関連表を削除する。削除後はScene scope情報を復元できないため、この情報損失をmigration文書とテストで明示する。

## テスト戦略

### Browser

- 422後にpendingが消え、修正済みbodyと新しいUUIDでPOSTできる。
- 応答消失とreloadでは同じUUID/bodyを再利用する。
- Turn ID取得後の失敗ではPOSTせずGET追跡を再開する。
- `state_version=2`、`latest_turn.committed_state_version=1`でも送信versionは2になる。
- polling失敗時にSSEとtimerが片付けられ、古いevent/responseがUIを変更しない。
- 正常なSSE接続中はpollingを開始しない。

### LLM Context

- `recent_gm`が`derived`、`recent_player`が`untrusted`、Action resultが`derived`になる。
- 過去GM文に命令風文字列があってもsystem instructionへ混入せず、Context dataにのみ残る。
- system instructionがContext内命令を信用しない旨を含む。

### PostgreSQLとGame flow

- 別Sceneの公開Entityと同Sceneの非公開EntityをLLM入力から除外する。
- Fake LLMが非公開、別Scene、到達不能な対象を返した場合、RNG呼出し、Action/Event追加、HP更新、`state_version`更新を行わない。
- `[有効な攻撃, 無効な攻撃]`も事前検証で全体を拒否し、最初のダイスを振らない。
- 有効な同Scene攻撃、回復後の攻撃、同一対象への連続攻撃を維持する。
- Campaign不一致の複合外部キー、`reachable => public`制約、upgrade/downgradeを検証する。

### 全体検証

- Python unit、contract、integration、PostgreSQL integration
- Node browser tests
- Ruff、mypy、package build
- migration headからのupgradeとdowngrade/upgrade

## 文書更新

- 新しいScene–Entity対応と対象選択規則をADRへ記録する。
- `docs/adr/README.md`へ新ADRを追加する。
- `docs/ai-trpg-contracts-v0.2.md`のsnapshotおよび対象検証を実装と一致させる。
- READMEの機能、制約、開発用seed説明を必要な範囲で更新する。

## 実装分割

共有変更を衝突させないため、次の順序で統合する。

1. UIの#11〜#13とbrowser回帰テスト。
2. Context信頼区分の#14とworker/統合テスト。
3. Scene–Entity schema、snapshot、事前検証の#15とPostgreSQLテスト。
4. 文書整合、全体検証、Issueコメントとクローズ。

各段階で局所テストを通してから統合し、全体検証前に`main`へ途中成果をpushしてもよい。ただしIssueは該当完了条件の検証が終わるまで閉じない。

## 却下した案

- すべてのPOST失敗でpendingを削除する: 受付済みだが応答を失った場合に重複Turnを作る。
- SSEとpollingを常時競争させる: 通信量と世代管理の複雑さが増える。
- Turnの確定versionをCampaignの現在versionとして使う: Turn外のCanonical更新で正しいversionを表現できない。
- 単一Scene CampaignをDBで強制する: Scene交代と履歴Sceneを許す現在の設計を狭める。
- Action履歴からScene所属を推測する: 未行動、退出、非公開、到達不能を区別できない。
- 座標や距離を含む汎用Scene engineを作る: MVPの到達可能性フラグには過剰である。

## 完了条件

- #11〜#15の再現ケースが回帰テストで失敗から成功へ変わる。
- 既存の冪等性、Campaign認可、Turn lease、Engine計算、atomic commitのテストが維持される。
- migration、ORM、Application port、worker、UI、文書が同じ契約を表す。
- 全体検証が成功し、ローカル`main`と`origin/main`が一致する。
- 各Issueへ検証結果とコミットSHAをコメントし、GitHub上でclosedになっている。
