# ADR-0014: 登録済み行動の直接実行と敵の反撃

- 日付: 2026-09-22
- 状態: 採用
- 関連: [0012](0012-scenario-definitions-and-progress.md)、[0013](0013-pydantic-ai-orchestration.md)

## 背景

短編を始めてから結末まで進めるには、選択した行動の意味を固定し、敵も行動する最小戦闘が必要になる。LLMに戦術やHP更新を任せず、既存の予算・lease・atomic保存・再送保証を使う。

## 決定

登録済み候補は`content={kind:"scenario_action",action_ref:"..."}`として送る。受付時にCampaignをロックし、認可・version・現在Scene・条件を検証する。既存requestの同一入力再送は現在Sceneの検証より先に処理する。Turnへrefとサーバー側labelを保存し、解決時にも再検証して既存Intent/Commandへ変換する。意図抽出LLMは呼ばず、描写は通常のDB予算で生成する。LLMが生成したChoiceと自由入力は従来どおり解釈する。

短編v2には入口、広間、記録庫、通路、祭壇、帰還を定義する。新規catalogはv2を返し、v1定義・保存済みrunは維持する。Sceneには公開NPC設定と、敵ref・戦闘開始flag・ダメージ式・敗北Endingを持つ型付きcombat設定を追加する。秘密の条件や未訪問SceneはLLMへ渡さない。

最初のプレイヤー攻撃で戦闘を開始する。以後、生存する敵は適用済みMechanical Turnの後に1回だけ反撃する。先行するプレイヤー行動のHP変更を反映してから既存Engineを呼ぶ。通常会話、未適用Turn、Scene遷移・Ending、敵HP0では反撃しない。PCのHP0は敗北Endingとする。戦闘中は交渉・隠密による突破候補を無効にする。

`CommitBundle.enemy_reactions`に型付きCommand/Result/RNGを渡す。Applicationで親ID、NPC/対象、件数、ダイス・HP遷移、描写との対応を検証し、プレイヤー行動の後にCanonical更新へ投影する。反撃は`actions`へ混ぜず、`EnemyReactionResolved` Eventのpayloadと保存済み描写入力へ格納する。Eventの`action_id`はnull、payload内に反撃IDを持つ。HP、反撃Event、Scenario進行、Turnを同じtransactionで確定する。

公開Turnに`enemy_reactions`（既定空）を、公開adventureに`combat`（既定null）を追加する。敵HPはDB由来とし、UIは反撃をプレイヤー結果と別表示する。描写再試行、GET、SSE、同一request再送でEngineを実行しない。

## 影響と制限

- migration0012で登録行動入力を追加する。旧text/choiceを維持し、新入力が残るdowngradeは拒否する。
- 敵はSceneにつき1体、反撃はTurnにつき最大1回。行動順・複数敵・LLM戦術は対象外。
- 回復は既存の自分用ポーションを使う。聖印は物語flagであり、汎用itemにはしない。
- 不正・未公開対象の拒否、stale worker、rollback、再送、描写retryの検証を維持する。
- groundingは数値・UUID・明示refの制限であり、自然言語の意味を完全に証明しない。会話品質と所要時間は別途プレイテストする。
