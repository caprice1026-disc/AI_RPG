# ADR-0011: Scene entity scopeをCanonical relationで固定する

- 状態: 採用
- 決定日: 2026-09-20

## 文脈と決定

`mvp_scene_entities` を、Scene membership、LLMへ渡す公開可視性、粗い攻撃到達可能性のCanonical relationとして採用する。行は `(campaign_id, scene_id, entity_id)` で一意とし、`is_public` と `is_attack_reachable` を持つ。`is_attack_reachable` は `is_public` を含意し、DBの `scene_entity_reachable_is_public` CHECKで守る。

workerはこのrelationから作る同じ可視集合を、LLMの参照一覧、参照解決、公開状態、描写入力に用いる。攻撃対象はさらに、公開かつ到達可能で、active refを持つ Characterに限る。Actorが所有するinventory／equipmentは所有権によってだけ可視にし、Actor-privateのままとする。所有物をScene-publicに昇格させない。

Mechanical planは、`snapshot(campaign_id, scene_id)` が返す初期snapshotに対して全件を検証してから、RNG、Engine、Action recordの作成を始める。初期snapshotで不正なplanは全体を適用しない。先行Actionの結果で後続Actionが不能になる既存の逐次的な`not_applicable`は維持する。

migration `0008_scene_entities` はtableと制約だけを追加する。既存deploymentにScene relationを推測してbackfillしないため、既存Entityは明示的に行を作るまで新しいScene可視性や攻撃対象にはならない。

## 採用理由

- Sceneごとの公開情報と攻撃可能性を、履歴やLLMの推測ではなく同じCanonical snapshotから決められる。
- 所有物のprivate性を保ったまま、公開Sceneの参照と区別できる。
- 全planの事前検証により、不正な後続Actionの前にdiceや部分的なActionを作らない。

## 却下した案

- **Campaignを単一Sceneとして扱う**: Scene遷移やSceneごとの公開範囲を表現できない。
- **履歴からScene relationを推測する**: 再現可能な可視性・攻撃可能性の根拠にならない。
- **座標・距離engineを導入する**: MVPに必要な粗い到達可能性を超え、移動API／UIと地図modelを先取りする。

## 影響

公開範囲は`mvp_scene_entities`とActor所有権で決める。`is_attack_reachable`は距離や座標を表さず、MVPに座標／距離engine、移動API／UIは追加しない。新規Scene relationの投入は明示的なseedまたは運用更新で行い、一般的なbackfillは行わない。
