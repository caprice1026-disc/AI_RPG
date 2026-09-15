# Turn受付P0検証 ExecPlan

## 目的

`docs/ai-trpg-contracts-v0.2.md` §4に従い、新規Turnを保存する前に最新の
Campaign membership、Actor操作権、`state_version`、Choice有効性を同じ
Campaign lock配下で検証する。再送はこれらの新規受付検証より先に既存結果へ戻す。

## 実装方針

- Campaign参照権はactive membershipで判定し、既存request検索より先に確認する。
- Applicationの事前認可portはCampaign参照権だけを扱う。新規TurnのActor操作権は
  PostgreSQL受付トランザクション内で`entities.controller_id`とactive membershipを再確認する。
- GMの全Actor操作権は現行docsに規則がないため追加しない。
- Campaign lockから取得した現在の`state_version`と`expected_state_version`を比較する。
- Choice入力は同Campaign・active Scene・Actor・state versionで、未失効かつ
  committed／描写完了済みの提示元Turnに属する場合だけ受理する。
- 新規Turn保存時だけ同Campaign・Actorの未失効Choiceを無効化する。
- 古いTurnのnarrationが後続Turnより遅れて完了した場合、narrationは保存するがChoiceは追加しない。
- migration、依存パッケージ、GM権限モデル、HTTP endpointは追加しない。

## 作業

- [x] 受付エラーをApplication portへ追加し、公開importを維持する。
- [x] Campaign参照権、Actor操作権、古いstate versionを拒否する実DBテストをREDにする。
- [x] Choiceの正常受理、scope／失効／version／提示元不正、受理時無効化を実DBテストでREDにする。
- [x] 後続Turnがある古いnarrationへChoiceを追加しない実DBテストをREDにする。
- [x] `PostgresTurnRepository`と`PostgresNarrationRepository`へ最小実装を追加してGREENにする。
- [x] unit testをCampaign参照権portと新しい問い合わせ順へ合わせる。
- [x] mutation確認後、全pytest、Ruff、mypy、lock同期、buildを実行する。
- [ ] 独立レビューでCritical／Importantを解消し、main統合後に全検証を再実行してpushする。

## 完了条件

- 無権限、古いversion、不正ChoiceではTurnもChoice失効もcommitされない。
- 正常なText／Choice入力はpending Turnを一つ作り、既存Choiceを同一transactionで失効する。
- 同一requestの再送は現在versionやChoice失効後も保存済み応答を返す。
- push後の`origin/main` SHAがローカルmainと一致する。
