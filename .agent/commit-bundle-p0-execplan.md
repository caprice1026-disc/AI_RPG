# Resolution CommitBundle P0 ExecPlan

## 目的

`docs/ai-trpg-contracts-v0.2.md` §5と既存の実装プロンプトに従い、
Mechanical Turnの確定前にCommand／Result／Event／narration inputを既存契約へ
一度だけ投影・検証する。Canonical、Turn、Action、Eventの途中失敗は同一transactionで
すべてrollbackする。

## 採用設計

- DTO全面置換は行わず、`commit_resolution`のSQL実行前に既存Pydantic契約へ投影する。
- DB制約は最後の防御として維持するが、ordinal、件数、親ID、型、相互対応は
  `InvalidCommitBundleError`として書込み前に拒否する。
- 互換性のため`canonical_changed`は残すが、`canonical_updates`の有無と一致することだけを
  検証する。version増分は更新内容から導出し、各更新は対象が存在して保存値が実際に
  変化する場合だけ受理する。
- Actionは1..Nの入力順、1件以上かつ受付時`max_actions`以下とする。
- CommandのCampaign／Turn／Actor／ordinal／kind／target／itemとActionRecordを一致させ、
  Result kindも一致させる。
- 各ActionにActionResolvedをちょうど1件要求し、DiceRolled／DamageAppliedを
  Result内のdice／damageと同じ順・内容で要求する。Narration eventは描写確定へ残す。
- MechanicalNarrationInputのversion、resolved_actions、max_actionsを確定内容と一致させる。
- lease discovery／renewal、worker orchestration、CONTEXT_CONFLICTの別transaction記録、
  routing、LLM provider、Engine拡張、migrationはこの作業へ含めない。

## 作業

- [x] 既存Command／Result／DomainEvent／MechanicalNarrationInputを使う投影検証をREDにする。
- [x] ordinal 1..N、件数、Scene／Turn／Actor、ActionResult、Event対応の拒否をREDにする。
- [x] Canonical更新有無からstate versionを導出し、対象なし・同値更新を拒否するテストをREDにする。
- [x] 正常確定でCanonical／Turn／Actions／Events／narration_inputを同時保存する。
- [x] Event INSERT失敗時に上記すべてがrollbackされる実PostgreSQLテストを追加する。
- [x] stale epoch、期限切れlease、古いstate version、committed再実行の既存挙動を実DBで固定する。
- [x] 全pytest、Ruff、mypy、lock同期、build、mutation確認を実行する。
- [x] 実装前の独立監査と差分レビューでCritical／Importantを解消し、main統合後に全検証を再実行する。

## 完了条件

- 不正BundleはCanonical、Campaign version／sequence、Turn、Action、Eventを変更しない。
- 正常BundleはAction順とEvent順を維持し、Canonical変更時だけstate versionを1増やす。
- Event保存失敗では同じ確定transactionの全書込みが残らない。
- push後の`origin/main` SHAがローカルmainと一致する。
