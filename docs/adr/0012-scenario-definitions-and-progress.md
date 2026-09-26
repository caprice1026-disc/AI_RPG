# ADR-0012: Scenario定義と実行状態を分離する

- 状態: 採用
- 決定日: 2026-09-20

このADRは固定短編v1・v2の判断を記録する。新規冒険で使う自由行動型v3は[ADR-0016](0016-bounded-open-scenario.md)を参照する。

## 文脈と決定

固定Scenarioの構造は、version付きの型付きJSONとしてリポジトリへ置く。定義にはScene、公開説明、登録済み行動、遷移条件、公開flag、Endingを含め、起動時に参照整合性を検証する。Campaignは開始時の`scenario_ref`と`scenario_version`を保存し、別versionの定義へ暗黙に切り替えない。

PostgreSQLには実行中の状態だけを保存する。`mvp_scenario_runs`はCampaignごとのScenario、version、status、Endingを持ち、`mvp_scenario_flags`は獲得済みflagを持つ。現在地には既存の`scenes.status`を使い、Scenario定義のScene `sequence`とCampaign Sceneの`sequence`を対応させる。専用のScene対応表は追加しない。

Scenario進行はMechanical commitだけで確定する。Action、Engineが作るCanonical更新、flag追加、Scene切替またはEnding、`ScenarioProgressed`、Campaignの`state_version`、Turn確定を同じtransactionへ含める。Scenarioだけが変わる登録済み行動でも`state_version`を増やし、技能判定や攻撃と同時に進む場合は一度だけ増やす。描写workerは保存済み結果を文章化するだけであり、Scenario進行を再適用しない。

Scenario runを持たない既存Campaignは従来どおり動作し、公開状態の`adventure`を`null`にする。一般的なbackfillは行わない。

## 採用理由

- 定義をversion付きJSONへ固定すると、同じScenarioを型検証したうえで再現できる。
- DBにはCampaignごとの可変状態だけが残り、定義の複製と更新漏れを避けられる。
- 既存のCampaign lock、state version、Action／Event／Turnのatomic確定をScenario進行にも適用できる。
- Scene `sequence`を対応キーに使えば、Stage 1の固定短編に新しい対応表は不要である。

## 却下した案

- **Scenario定義全体をDBへ保存する**: Stage 1では編集APIや運用中の定義変更を扱わず、JSONとDBの二重管理になる。
- **LLMに遷移とEndingを直接決めさせる**: 未検証の遷移や状態変更をCanonicalへ持ち込めない。v3でもLLMの提案をApplicationが検証してから確定する。
- **自由なflag mutationを許す**: 定義に存在しない状態を保存でき、公開文と進行条件の対応を検証できない。
- **Scenario専用のScene対応表を追加する**: 一つの固定Scenarioを扱う段階では、既存の一意なScene `sequence`で対応できる。

## 影響

Scenario定義のversionを変更するときは、進行中runの扱いを明示したmigrationまたは新しい定義versionが必要になる。`0009_scenario_progress`をdowngradeするとrunとflagを削除するため、再度upgradeしても進行状態は復元されない。必要な状態はdowngrade前に退避し、再投入しなければならない。

`0010_scenario_action_kind`からのdowngradeは、`scenario_action`行が存在する間は失敗する。通常はforward-fixで対応する。downgradeが必要な場合は、必要な履歴とScenario状態を保全先へアーカイブしてからimmutableな`scenario_action`行を除去する、明示的な特権保守手順を先に実施する。この操作は通常のApplication資格情報には許可せず、Applicationが履歴を削除できることを意味しない。

Stage 1は組込みの`ruined_chapel`だけを対象とする。開始画面、一般利用者向けCampaign作成、実LLMでの調整、敵の反撃、ブラウザログインはこの決定に含めない。
