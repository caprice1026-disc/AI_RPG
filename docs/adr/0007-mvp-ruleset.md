# ADR-0007: `mvp_v1` rulesetの最小ゲームルール

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

ゲームルールを暗黙の「一般的なTRPG」に依存させず、Campaign作成時に不変の `ruleset_id = "mvp_v1"` として次を固定する。数値計算はすべてGame Engineが行い、LLMは値を決定しない。将来のrulesetは別IDとversionで登録し、既存Campaignの意味を上書きしない。

### 共通ターン規則

- 1 Turnは入力順に解決する0〜3 Actionsを持つ。Mechanicalでは1〜3、Narrativeでは0とする。
- 同一TurnのActionsは一つのtransactionで確定する。先行Actionの結果を作業用状態へ反映してから次を検証する。
- 入力から複数の意図を抽出できても、3件を超える部分は実行せず確認を返す。1件のActionを複数に水増ししない。
- 初期検証で不正な計画は全体を適用しない。先行Actionの正常結果によって後続Actionだけが不能になった場合、そのActionを `not_applicable` として後続を順に評価する。

### HP

- `max_hp` は1以上の整数、`current_hp` は常に **0以上 `max_hp` 以下**とする。従ってHP下限は0であり、負数を保存しない。
- damageは軽減後の0以上の整数とし、`current_hp = max(0, current_hp - damage)`。healingは0以上で、`current_hp = min(max_hp, current_hp + healing)`。
- 0 HPは `incapacitated` を意味し、攻撃、技能判定、アイテム使用のactorにはなれない。死亡判定、蘇生、temporary HP、damage type、resistanceは `mvp_v1` の対象外とする。

### 攻撃

攻撃者、到達可能な対象、所有・装備したweaponが必要である。`1d20 + attack_bonus` を1回振り、合計が対象の `defense` **以上なら命中**する。命中時はweaponの `damage_expression + damage_bonus` を評価し、0未満ならdamageを0に丸めてHPへ適用する。失敗時はdamage 0とする。natural 1/20による自動失敗・自動成功・critical、advantage、範囲攻撃は採用しない。非武装はweapon定義 `unarmed`（damage `1d2`）へ正規化する。

### 技能判定

`mvp_v1` の技能一覧は次の5つに固定する。

| skill_ref | 意味 |
| --- | --- |
| `athletics` | 筋力を使う運動 |
| `acrobatics` | 敏捷性、平衡、身のこなし |
| `perception` | 観察、探索、気配の察知 |
| `stealth` | 隠密、静かな移動 |
| `persuasion` | 交渉、説得 |

Applicationが目的と状況から技能と難易度 `DC` をruleset定義済みの選択肢として検証する。任意名の技能やLLMが生成したDCは受理しない。`1d20 + skill_modifier` がDC以上ならsuccess、それ以外はfailureとする。標準DCはeasy 8、normal 12、hard 16で、Scene定義はこのいずれかを参照する。対抗判定、critical、受動判定は対象外とする。

### アイテム使用

MVPで使用可能なのはruleset registryに効果が登録された**消耗品**だけとする。actorが所有し、残数が1以上で、対象が有効なとき、登録済み効果をEngineが解決してから残数を1減らす。初期登録は `healing_potion` のみで、対象は使用者、効果は `1d6 + 2` HP回復とする。0 HPでは使用できない。前提不成立やEngine失敗時は消費しない。LLMが自由記述した効果は適用しない。

### ダイス式

受理する文法は空白を除去した `NdS` または `NdS+M` / `NdS-M` のみとする。`N` は1〜20、`S` は2〜100、`M` は0〜100の10進整数で、`d` は小文字へ正規化する。各dieは独立に1〜Sを生成し、`total = sum(rolls) + signed_modifier` とする。括弧、複数項、除算、変数、exploding dice、暗黙の `d20` は拒否する。式、個別出目、modifier、total、乱数源のversionを記録する。

## 採用理由

- HP下限と技能一覧をruleset内で固定し、Campaignごとに同じ入力を決定的に検証できる。
- d20による統一判定と単純な消耗品だけで、攻撃・判定・状態更新・乱数・所有権のend-to-end経路を検証できる。
- 例外規則を減らし、LLMではなくEngineが結果を決める原則を明瞭にできる。

## 却下した案

- **既存商用TRPGルールの全面採用**: ライセンス、例外規則、データ量がMVPに過剰である。
- **技能を自由文字列にする**: LLM表記揺れと未定義modifierにより決定的検証ができない。
- **HPを負数まで許す／死亡判定を導入する**: 下限と回復の意味が複雑になり、最小仕様を超える。
- **任意のダイス計算式をevalする**: 安全性と再現可能な構文検証を損なう。

## 後から交換できる境界

Game Engineは `Ruleset` Protocol（行動検証、解決、dice parser、event schema registry）を `ruleset_id` で取得する。Canonical recordとeventにはruleset ID/schema versionを残す。新しい技能、HP規則、item効果は新rulesetとして追加し、`mvp_v1` のclassや定数をrouter、HTTP、LLM adapterへ埋め込まない。

## 影響

Campaign作成後のruleset変更は通常の設定変更として許可しない。移行する場合はCanonical Stateと過去eventを変換する明示的なmigrationとして別ADRを作る。
