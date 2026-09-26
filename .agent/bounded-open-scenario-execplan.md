# 自由行動型シナリオ実装 ExecPlan

## 目的と境界

[ADR-0016](../docs/adr/0016-bounded-open-scenario.md)を、現在の廃礼拝堂の次versionで実際に遊べる機能として実装する。開始・再開・自由入力・失敗後の状態変化・結末まで通し、既存の固定version 1/2と`mvp_v1` Campaignは移行しない。既存のCampaign lock、worker lease、DB呼出予算、Action/Event/Turnのatomic確定、同一要求再送を維持する。

ADR-0016はユーザーが確認済みの設計である。PLANS.mdはこのリポジトリに存在しないため、既存の`.agent`計画と同じ形式を用い、重複する設計書は作らない。コード変更は失敗するテストから始める。

## 実装上の具体化

- 新しい短編は`ruined_chapel` version 3、規則は`mvp_v2`とする。既存のJSONと規則は変更しない。入口・広間・記録庫・通路・祭壇・帰路を主要地点として残すが、登録済み行動を移動の唯一の条件にしない。
- 能力は`strength`、`agility`、`insight`、`presence`。斥候と守護者の基礎値に計2点を0〜2点ずつ配分し、合計後の上限は3。得意技能を既存5技能から一つ選び、対応する判定へ+2する。選択は開始payloadの一部として冪等に保存する。
- 自由入力からの提案は目的・方法・判定要否・能力・任意の得意技能・難易度`easy/normal/hard`・成功/失敗の効果を型付きで受け取る。状態効果は主要地点への移動、結末分類、既存flag、現在地点内の公開事実追加、警戒増減に限定する。HP/在庫/報酬の任意更新を許さない。
- 自由行動では意味のある実行一回につきゲーム内時間を1進める。入力確認・不正提案・重大リスクのキャンセルでは進めない。難易度と両結果をEngineの乱数利用より先に検証する。
- 新規の事実は場所・人物・手掛かり・経路のいずれかとして現在の主要地点に結び付けて保存する。公開Contextと状態APIには確定済み事実だけを含め、固定された依頼品の所在・人物の目的・領域外の場所を新しい正本として追加しない。
- 重大リスクのプレビューは、所有者・元Turn・state versionに結び付けた保存済み提案として扱う。確認Turnはその提案を再利用し、LLMに再評価させない。キャンセルは新規行動を作らない。

## Task 1: 定義・永続化・キャラクター作成

担当: `scenarios/models.py`、version 3 JSON/catalog、`contracts/adventures.py`、`application/adventures.py`、`application/ports/adventures.py`、`infrastructure/postgres/adventures.py`、`models.py`、新migration、関連unit/PostgreSQL tests。

- [x] version 3の境界・保護事実・結末分類の型検証テストを失敗させ、定義とcatalogを追加する。
- [x] 2点配分、上限、得意技能、既存versionでの入力拒否・開始再送一致のテストを失敗させ、開始契約と`mvp_v2`保存を追加する。
- [x] 能力値、動的事実、警戒・経過行動数、リスク提案のmigration/ORMを追加し、空DBでupgradeと既存runの互換を確認する。

## Task 2: 自由行動の型と規則

担当: `contracts/llm_decisions.py`、`contracts/context.py`、`domain/commands.py`、`engine/ruleset.py`、`application/scenarios.py`、`application/routing.py`、`llm/prompts.py`、Fake/Agent、unit/contract tests。

- [x] 長椅子の足場化、判定不要の移動、失敗時の警戒増加、領域外の撤退を、選択肢照合なしで解釈する失敗テストを追加する。
- [x] 新rulesetの能力判定と技能補正をEngineで解決し、`mvp_v1`の固定技能判定が不変であることを確認する。
- [x] 定義済みの保護事実、現在地、効果種類、難易度、結果の整合性をApplicationで検証し、無効提案ではダイスを振らない。

## Task 3: worker・atomic確定・再開

担当: `application/workers.py`、`application/resolution.py`、`application/ports/repositories.py`、`infrastructure/postgres/repositories.py`、必要なEvent/DTO、unit/PostgreSQL tests。

- [x] 自由行動と生成事実・経過時間・警戒を一transactionで保存し、Event・公開状態・描写入力を同じ結果から作る失敗テストを追加する。
- [x] 古いstate version/lease/actor権限、Event INSERT失敗、同一Turn再送、描写再試行で二重更新しないことを検証する。
- [x] 重大リスクのみ保存済みプレビューを返し、確認後も最新状態を再検証する。キャンセル・通常判定・危険判定の各経路をテストする。

## Task 4: プレイ画面と受入

担当: `frontend/src/contracts.ts`、`game.ts`、`App.vue`、必要なCSS、frontend tests、READMEと競合するdocs。

- [x] 開始画面で能力2点の配分と得意技能を選び、再送payloadを固定する。公開状態で能力・警戒・時間・生成事実を表示する。
- [x] 重大リスクの確認・キャンセルを画面へ接続し、自由入力を選択肢の文言一致へ戻さない。
- [x] Fakeで入口→長椅子の別経路→失敗後の再挑戦→再開→別の解決または撤退、早期達成を通す。実モデルは任意の補助確認とし、Fake/DBの受入を代替しない。
- [x] README/開発ガイド/旧固定短編仕様のうち現行説明と矛盾する箇所を更新し、旧versionの履歴記録は削除しない。
- [x] Python unit/contract/PostgreSQL、frontend test/typecheck/build、Ruff、mypy、migration、`git diff --check`を実行し、実行できない境界を結果へ記録する。

## 検証の基点

- 開始commit: `58a838c`。作業開始時の`main`はclean。
- 2026-09-26: `.venv/Scripts/python.exe -m pytest tests/unit tests/contract -q -p no:cacheprovider`で503 passed。
- Docker APIの通常権限での接続は拒否された。PostgreSQL統合確認時に別途接続状態を確認する。
- 2026-09-26: Python unit/contract 519件、Vue 87件・型検査・ビルド、Ruff、mypy、`git diff --check`を確認。PostgreSQLのv3開始/保存/リスク確認と通しプレイは専用DBで確認。全PostgreSQL統合238件の初回結果は230成功・8失敗。8件は新migration revision／追加DTO項目の旧期待値と、旧Contextへv3項目が混ざる問題で、修正後の該当8件を再実行して全件成功。全238件の再実行はしていない。
- レビュー後、保護事実の参照衝突、登録済み撤退・戦闘の重大リスク確認、v3の複数行動拒否、旧v1/v2の警戒表示非公開、完了後の能力表示を修正・回帰テスト化。登録済み攻撃の確認から確定まで実DBで追加確認。実Geminiでは専用`ai_rpg_test_gemini_v3`で入場→長椅子を足場に別経路へ進む→祭壇で交渉→別の解決による結末まで通した。確定した生成経路・結果描写・再接続後の履歴5件・DB予約6回を確認。通常の長椅子判定がモデルの危険文言で確認待ちにならないよう効果ベース判定へ修正した。実プレイの試行には交渉が不適用となって撤退した例もあり、自由文解釈は決定的ではない。一時DBは空を確認して削除した。人による試遊は未実施。
- 追加レビューで、核心の祭壇を否定する別表現と、戦闘中に退室して反撃が起きない行動へのHP警告を発見。生成事実では核心の場所への言及を制限し、リスク判定を戦闘の離脱条件に合わせた。両方を失敗→修正後成功の単体テストで確認した。
- 全PostgreSQL統合suiteは241件成功（636.76秒）。実行中に追加レビュー修正を行ったため、その2件の最終挙動は別途の単体・重点PostgreSQLテストで確認する。最終のPython unit/contractは524件成功。
- 追加レビュー後の最終コードでPostgreSQLの冒険16件が成功。Gemini 3.5 Flashの再試遊でも入場→長椅子から別経路→祭壇で交渉→`alternative_resolution`へ到達し、生成事実1件、DB予約6回、再接続後の履歴5件を確認した。
