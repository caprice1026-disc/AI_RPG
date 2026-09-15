# Turn受付Idempotency ExecPlan

## 目的

`docs/ai-trpg-contracts-v0.2.md` §4に従い、同一の
`(campaign_id, principal_id, request_id)` を安全に再送できるようにする。
同じ正規化入力は保存済みTurnの現在状態を返し、異なる入力は
`IDEMPOTENCY_CONFLICT`、別の未解決Turnは`TURN_IN_PROGRESS`として拒否する。

## 範囲

- 既存のPostgreSQL UNIQUE制約と短いCampaign lockを維持する。
- `ON CONFLICT DO NOTHING`後に同一requestを再読込し、保存JSONとSHA-256の両方を比較する。
- 保存済みTurn、Action、Choiceから公開`TurnResponse`を再構築する。
- 認可、state_version、Choice有効性の追加検査は次の独立作業とし、今回は変更しない。
- migration、依存パッケージ、新しいRepository階層は追加しない。

## 実装手順

1. 実DBテストを先に追加し、同一入力の再送、異なる入力、保存JSON/hash不一致、
   別の未解決Turn、同時再送、確定済みTurnResponseの復元が現状で失敗することを確認する。
2. ApplicationのRepository契約へ`IdempotencyConflictError`と
   `TurnInProgressError`を追加し、公開importを維持する。
3. `PostgresTurnRepository.add()`で既存requestをactive Scene検索より先に読み、
   同一性を確認する。INSERT競合時も同じ照合を行い、transactionを失敗状態にしない。
4. 保存済みTurnのstatus、narration、Action結果、未失効Choiceを`TurnResponse`へ投影する。
5. 既存mockテストを新しい問い合わせ順へ合わせ、全検証を実施する。

## 検証

- 追加テストをREDからGREENへ進める。
- 使い捨てPostgreSQL 17で全pytestを実行し、skipを成功へ含めない。
- `ruff check .`、`mypy src tests`、`uv sync --frozen`、`uv build`、
  `git diff --check`を実行する。
- mainへ統合後も全テストを再実行し、push後のremote SHAを確認する。
