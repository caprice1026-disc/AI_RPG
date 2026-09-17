# Fake LLM基盤からプレイ可能なMVPへ進める ExecPlan

> **実装担当向け:** この計画はTDDで上から順に実行する。各チェックポイントで対象テスト、全pytest、Ruff、mypyを確認し、既存のCampaign→Turnロック順、epoch、冪等性、DB永続LLM予算を崩さない。

**目的:** レビューで再現した3件を修正し、独立processのFake実行、複数Turn Context、OpenAI adapter、最小プレイ画面、攻撃・回復item、SSEまでを順に接続する。

**アーキテクチャ:** API、解決worker、描写workerは同じApplication use caseとPostgreSQL durable queueを共有し、別processで起動する。LLMは型付きIntentと描写だけを返し、Game EngineとApplication projectionだけがCanonical Stateを変更する。公開Contextと公開eventは許可リストで組み立てる。

**技術:** Python 3.11、FastAPI、Pydantic 2、SQLAlchemy 2、PostgreSQL、Alembic、pytest、Ruff、mypy。

## 全体制約

- READMEの未commit差分を保持し、実装の進捗に合わせて内容を更新する。
- 新しいbroker、frontend framework、provider SDKは追加せず、既存依存または標準機能を優先する。
- providerへの物理request前にDB予算を予約し、timeoutや不正出力でも返却しない。
- leaseとdeadlineはPostgreSQLの`clock_timestamp()`で判定し、通常結果は期限後に保存しない。
- fallbackだけは期限切れphaseを終端化するため、現在ownerであることを確認して保存できる。
- 既存の認可再検証、Campaign単位排他、Action/Event atomicity、stale epoch拒否を維持する。

## チェックポイント1: レビュー3件

**変更:** `src/ai_rpg/application/workers.py`、`src/ai_rpg/application/ports/repositories.py`、`src/ai_rpg/infrastructure/postgres/repositories.py`、`tests/integration/postgres/test_migrations.py`

- [x] Narrative/Mechanicalの判定前入力にScene判定条件の`public_description`が含まれず、成功後の`MechanicalNarrationInput.public_state_after`だけに含まれる回帰テストをREDにする。
- [x] 確認質問の`not_applied`、`narration_status='completed'`、質問文、`GMNarrationGenerated`を一transactionで保存し、同時に描写workerが取得できない回帰テストをREDにする。
- [x] `commit_resolution`、Narrative通常確定、描写通常保存をdeadline後に拒否し、worker経路では`failed/fallback`へ終端化する回帰テストをREDにする。
- [x] `_public_context`から成功条件を除き、`finalize_not_applied`へ質問文とevent追記を統合し、通常保存条件へphase deadlineを追加する。
- [x] 既存の技能判定、確認質問、timeout、stale owner、rollbackテストを含むPostgreSQL suiteをGREENにする。

## チェックポイント2: 独立processで動くFake版

**作成・変更:** `src/ai_rpg/runtime.py`、`src/ai_rpg/cli.py`、`src/ai_rpg/api/app.py`、`pyproject.toml`、`tests/integration/postgres/test_runtime.py`、`README.md`

- [x] `ai-rpg seed-dev`で固定Campaign、principal、PC、Scene、技能条件を冪等投入するテストをREDにする。
- [x] `ai-rpg api`、`ai-rpg resolution-worker`、`ai-rpg narration-worker`が同じ環境設定から別processとして起動し、poll間隔内に一往復するテストをREDにする。
- [x] worker loopは`run_once(None)`を繰り返し、未取得時だけ短く待ち、SIGINT/SIGTERMで安全に終了する最小実装にする。
- [x] processを再実行して同じTurnを回収し、確定済みAction／Eventを増やさない統合テストをGREENにする。lease期限切れ回収はチェックポイント1のworker交代テストで維持する。
- [x] READMEへfixture投入、3process起動、POST/GET、停止・再起動手順を追加する。

## チェックポイント3: 複数Turn Context

**変更:** `src/ai_rpg/application/ports/repositories.py`、`src/ai_rpg/application/workers.py`、`src/ai_rpg/infrastructure/postgres/repositories.py`、`src/ai_rpg/contracts/context.py`、`tests/integration/postgres/test_migrations.py`

- [x] 完了済みTurnだけを新しい順で上限取得し、時系列順の公開`recent_messages`へ変換するテストをREDにする。
- [x] player入力、保存済みnarration、公開Action結果だけを含め、内部failure code、秘密の判定条件、未完了Turnを除外するqueryを実装する。
- [x] 直前の確認質問と次の回答をContextへ含め、回答Turnを同じactor/Sceneの入力として解釈できるFakeシナリオをGREENにする。
- [x] `AIRPG_RECENT_MESSAGES_LIMIT=0..100`をworker processへ渡し、取得上限と順序を確認する。

## チェックポイント4: ADR-0006 OpenAI adapter

**作成・変更:** `src/ai_rpg/llm/openai.py`、`src/ai_rpg/llm/__init__.py`、`src/ai_rpg/config.py`、`src/ai_rpg/runtime.py`、`pyproject.toml`、`tests/contract/test_openai_transport.py`

- [x] HTTP transportを差し替えたcontract testで、model、instruction、input、JSON Schema、timeout、`Authorization`を確認する。
- [x] SDK自動retryを持たない単発requestとしてResponses APIを呼び、refusal、HTTP失敗、空/不正JSONをApplicationで分類できる例外へ変換する。
- [x] API keyは`AIRPG_OPENAI_API_KEY`からだけ読み、Fake実行では不要、本番adapter選択時だけfail fastにする。
- [x] 保存済み確定結果にない数値・明示entity/item refを描写した出力を拒否する事実整合性評価を追加し、予算内retryまたはfallbackへ送る。

## チェックポイント5: 最小プレイ画面

**作成・変更:** `src/ai_rpg/api/static/index.html`、`src/ai_rpg/api/app.py`、`tests/integration/test_api.py`、`README.md`

- [x] 単一画面でCampaign/Actor入力、Turn送信、GET polling、状態・判定・描写・再試行案内を表示するブラウザ試験をREDにする。
- [x] FastAPIから依存追加なしのHTML/CSS/JavaScriptを配信し、送信中の二重送信を防ぎ、終端状態までpollする。
- [x] label、focus、status live region、keyboard操作、狭い画面を最低限満たす。
- [x] 本番認証未接続時は401を表示し、dev principalの有効化は明示的な開発設定に限定する。

## チェックポイント6: 攻撃・回復item、その後SSE

**変更:** `src/ai_rpg/contracts/llm_decisions.py`、`src/ai_rpg/domain/commands.py`、`src/ai_rpg/engine/ruleset.py`、`src/ai_rpg/application/workers.py`、`src/ai_rpg/infrastructure/postgres/repositories.py`、`src/ai_rpg/api/app.py`、`tests/integration/postgres/test_migrations.py`、`tests/integration/test_api.py`

- [x] 型付きAttack/UseItem Intentを登録済みentity、weapon、inventoryへ解決し、所有権、対象、0 HP、残数をApplicationで拒否するテストをREDにする。
- [x] Engineの既存`AttackCommand`/`UseItemCommand`経路をworkerへ接続し、damage、`HealingApplied`、`ItemConsumed`を一transactionで保存する。
- [x] 命中失敗、HP下限/上限、在庫0、stale version、event失敗rollback、同一request再送を実PostgreSQLで確認する。
- [x] 認可済みCampaign eventをsequence順に投影する`PublicEvent` queryとSSE endpointを追加する。
- [x] `Last-Event-ID`再送、at-least-once重複、15秒heartbeat、membership失効、遅い接続の切断をAPI testで確認する。
- [x] プレイ画面をpolling fallbackを残したままSSE更新へ切り替える。

## 各チェックポイントの検証

1. 追加したテストを単独実行し、実装前の期待した失敗と実装後の成功を記録する。
2. `python -m pytest -q -p no:cacheprovider`を専用PostgreSQL付きで実行し、skipを成功扱いしない。
3. `python -m ruff check .`、`python -m mypy src`、`uv build`を実行する。
4. `git diff --check`と`git status -sb`でREADMEを含む意図したファイルだけを確認する。
5. checkpoint単位でcommitし、pushする場合は`origin/main`のSHAを照合する。

## 進捗記録

| チェックポイント | 実装 | PostgreSQL結合検証 | 証跡 |
| --- | --- | --- | --- |
| 1 レビュー3件 | 完了 | 完了 | `176 passed`、Ruff、mypy（2026-09-17） |
| 2 独立Fake process | 完了 | 完了 | 実process受入を含む`179 passed`（2026-09-17） |
| 3 複数Turn Context | 完了 | 完了 | 公開履歴・確認回答を含む`184 passed`（2026-09-17） |
| 4 OpenAI adapter | 完了 | 完了 | HTTP contract・拒否・事実逸脱を含む`200 passed`（2026-09-17） |
| 5 最小プレイ画面 | 完了 | 完了 | `201 passed`、実browserで送信・終端・401・390px表示を確認（2026-09-17） |
| 6 攻撃・回復item・SSE | 完了 | 完了 | `221 passed`、型付きAction、公開event、実browserのSSE／polling fallbackを確認（2026-09-17） |
