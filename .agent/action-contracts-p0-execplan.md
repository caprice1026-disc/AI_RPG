# Action contract P0 ExecPlan

## 目的

`77a0193`で残る同名Command／Result、任意Mapping、HP tuple更新を解消し、
Engineの型付き結果から永続化projectionを作る責務をApplicationへ移す。

## 採用設計

- `domain.commands`／`domain.results`をゲーム内部の正本とし、Engineも同じ型を返す。
- LLM出力は既存`ActionIntent`、保存単位は型付き`ActionRecord`に限定する。
- state changeは`DamageApplied`／`HealingApplied`／`ItemConsumed`の閉じたunionとする。
- Application mapperがAction、Event、Narration、Canonical mutationの対応を検証・投影する。
- InfrastructureはDB行のlock、保存前値の照合、SQL実行だけを担当する。
- version変更有無は有効なCanonical mutationから導出する。

## 作業

- [x] 型付き契約とApplication projectionの失敗テストを追加する。
- [x] Engineを正本Command／Resultへ移行し、旧同名dataclassを削除する。
- [x] CommitBundleから任意Mapping、canonical flag、HP tupleを除く。
- [x] RepositoryからPydantic復元とHP計算をApplication projectionへ移す。
- [x] damage、healing、item consumptionの実DB保存とrollbackを検証する。
- [x] 全pytest、Ruff、mypy、build、diffを検証する。

## 完了条件

- Engine、Application、InfrastructureでCommand／Resultの意味が一意である。
- healingとitem consumptionを自由形式の辞書なしで同一transactionに保存できる。
- 既存の並行・冪等・stale owner・rollback保証が維持される。
