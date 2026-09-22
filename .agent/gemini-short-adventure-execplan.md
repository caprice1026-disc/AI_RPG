# Pydantic AI移行と後続の短編拡張 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent tasks and scoped review. Shared runtime integration stays with the controller. Follow repository .AGENTS.md; no repeated approval gate for this authorized implementation.

**Goal:** Task 1は31db398でmain反映済み。今回の依頼によりTask 2〜4を実行し、固定短編の開始・再開、実LLMによる自由入力、敵の反撃、勝利・敗北・撤退後の結末までつなぐ。

**Architecture:** 会話、意図抽出、確定結果の描写を用途別のPydantic AI Agentとして構成する。Applicationは用途別portから型付き結果を受け取り、Agent間の受け渡しを制御する。provider付きモデル名を設定で切り替える。後続Task 2〜4では、登録済み行動の直接入力と、Engineで計算した敵反撃のatomic保存を追加する。

**Tech Stack:** Python、Pydantic AI slim（Google/OpenAI extras）、httpx、SQLAlchemy、Alembic、PostgreSQL、既存HTML/JavaScript。frontend frameworkは追加しない。

## Global Constraints

- 今回のbase main: 31db398267d80395c331971e2815ea29364f5f90。対象worktreeは .worktrees/short-adventure-stage4、branch codex/short-adventure-stage4。Task 1の旧baseはf45d23e。
- Pydantic AIと必要なprovider依存に限定する。既存OIDC認証・事前登録principal・subject隔離を維持する。ブラウザログイン、課金、RAG、独立NPC Agent、マルチプレイは対象外。
- LLMは意図と描写だけを生成し、HP、難易度、状態遷移、敵戦術を決定しない。未公開Scene・flags条件・他PC所持品をContextへ出さない。
- main checkoutの.envにあるGEMINI_API_KEYを使用する。値を表示・ログ・Git・テストfixtureへ保存しない。公式APIへの認証header以外へ送らない。
- 実モデルは gemini-3.5-flash のみ。別モデル/providerへの暗黙fallbackなし。物理requestの暗黙retryなし。既存DB予算予約・lease/epoch・deadline・冪等性・Campaign先行lockを維持する。
- 既存ruined_chapel v1の意味は変更しない。新版v2の登録・catalog変更は後続Task 3で行う。
- 20〜30分はプレイ時間の目標であり、今回の自動受入を実プレイヤーの所要時間実測として扱わない。短編v2の会話・探索・複数の判断は後続Task 3、時間と楽しさの評価はプレイテストで扱う。
- 実DBテストはlocalhost:55432の専用ai_rpg_testだけで実行する。実モデル/browser検証は別DB。実モデルの確認は最大30物理request、出力上限付きの短編検証に限定し、上限で打ち切る。失敗をFake成功で置き換えない。
- サブエージェントへ秘密値を渡さない。共通ファイルの同時編集と同じDBへの並行testsを禁止する。段階ごとのファイル所有範囲を明示する。

## Task 1: Pydantic AIによるAIオーケストレーション

状態: 31db398でmainへの統合・pushとSHA照合まで完了。再実装しない。依存はPydantic AI 2.46.0をlockした。

移行の分担: controllerは用途別Protocol・出力型・Agent・workerとFakeを実装する。独立した設定/runtime担当はモデルfactory・設定・CLI・関連unit testを編集する。DB予算予約はworkerが各port呼出直前に一度だけ行い、Agent/Fakeは予約を行わない。Agentはretries=0、UsageLimits(request_limit=1)、toolなし。runtimeがGoogle/OpenAI SDK clientを生成・終了し、SDK retryを無効化する。

順序: 出力型とportの契約テスト → 結果描写Agent → 意図/会話Agentとworker/Fake → 設定/runtime統合 → HTTP mock・実PostgreSQL・実Gemini検証 → ADR/README → reviewとmain統合。現在のScripted/Development Fakeは用途別portを直接実装し、Agent自体はFunctionModelとHTTP mockで検証する。

担当: controllerが用途別portとworker境界を定め、その後に独立したAgent実装を委任する。Task 2とworkers.pyを同時編集しない。対象はapplication/ports、llm、workers.pyのAI呼出部分、config.py、runtime.py、対応tests、pyproject.toml、uv.lock、.env.exampleとする。

Interfaces: NarrativeGenerator、IntentExtractor、ResultNarratorをApplication側の用途別portとする。実装側のPydantic AI Agentが、型付きoutput_type、許可済みContextの依存注入、指示、provider/model選択、出力検証を担う。Agent間の受け渡しはApplicationが制御し、Game EngineやDB書込みをLLMのtoolへ公開しない。

- [x] ADR-0006を置換する新ADRを追加し、READMEと契約文書に責務分担を明記する。旧ADRと実装履歴は削除せず、Pydantic v2による型検証とPydantic AIの採用を区別する。
- [x] NarrativeDecision、MechanicalDecision、MechanicalNarrationDraftを用途別Agentの型付き出力へ接続する。既存の参照・権限・grounding検証とTurnごとのAction上限を維持する。Pydantic AIのFunctionModel等でFakeも同じAgent経路をテストする。
- [x] モデルはprovider:model形式の設定から選び、初期値をGemini 3.5 Flashとする。FAST/QUALITY/BACKGROUNDのoverrideを維持する。Google/OpenAI切替とproviderごとの認証設定をテストし、他providerに必要なextra・適合試験を文書化する。
- [x] 呼出予算はDBを正本とする。AgentとSDKの暗黙retryを無効化し、物理request前の予約・epoch確認を迂回しない。Agentのメモリ内UsageLimitsだけで永続予算を置き換えない。修復は既存の上限付きworker経路から明示的に行う。
- [x] timeout、network、HTTP429/5xx、拒否、不正出力、不完全出力、未予約の追加request拒否を実際のAgentとprovider HTTP境界でテストする。秘密値・raw responseを例外やtelemetryへ流さない。model呼出失敗でEngineや確定済み処理を再実行しない。
- [x] Pydantic AIを通常経路に接続後、独自OpenAIResponsesTransportと重複したschema変換を置換する。同等のprovider contract coverageを残し、旧経路を通常runtimeへ併存させない。実API・DBを含む検証を終えてから実装済みと記録する。

## Task 2: 登録済み行動の直接実行と公開Context

状態: 実装・検証・レビューとmainへの統合・pushが完了（実装commit 6cc154c）。

担当: controller。対象: contracts/player_turn.py、ports/repositories.py、application/turns.py・workers.py・scenarios.py、postgres/models.py・repositories.py・adventures.py、migration0012、関連tests。UIはTask4が担当。

Interfaces: 新規入力はcontent={kind:'scenario_action', action_ref:'enter_chapel'}。request_id/expected_state_version/actor_idは既存。選択したaction_refとサーバー側labelをTurnへ保存し、ResolutionWorkItemへ渡す。進行/戦闘側は既存Intent/Commandに正規化した結果を受け取る。

- [x] 同一入力再送・payload相違・古いversion・別Scene/条件不成立・偽refを実DBでテストする。受付済み同一requestのreplayを現在のScene検証より優先し、応答喪失後も同じTurnを返す。
- [x] typed入力とmigrationを追加する。元のtext/choiceを維持する。選択肢ラベルを再解釈せず、現在の定義・flags・actor権限から登録済みDirect/Skill/Attack Intentを作る。解決前にも再検証し、選択行動のintent用LLM呼出は0回、描写だけ既存予算を消費する。
- [x] scene公開説明、目的、公開facts、登録行動のkind/ref/対象/技能、PC自身のHP・装備・所持品、直近完了会話をContextへ含める。少数の公開NPC会話設定をSceneに置く。秘密の遷移条件/未訪問情報は出さない。
- [x] 自由入力で会話か判定かを選び、action_refとスキル/攻撃の対応を明示するpromptにする。解釈不能な入力は確認/未適用へ戻し、状態を捏造しない。
- [x] 関連unit/contract/DB testsをGREENにしcommit。Task3との共有箇所はcontrollerが逐次統合する。

## Task 3: version付き短編v2と最小戦闘

状態: 実装・検証・レビューとmainへの統合・pushが完了（実装commit 6cc154c）。

担当: controller。対象: scenarios/models.py・catalog.py・ruined_chapel_v2.json、application/combat.py（必要な純粋処理のみ）、workers.py・resolution.py、domain/results.py・events.py、contracts/responses.py、postgres/repositories.py、narration_grounding.py、関連tests。

Interfaces: CommitBundleに既定空のenemy_reactionsを持たせ、型付きEnemyReactionにcommand/result/rngを保持する。既存プレイヤーactionsの1〜3件制限・actor一致を緩めない。反撃はEnemyReactionResolved Event（action_id=None、payload内に反撃のID・攻撃者・対象・結果・RNG）と保存済み描写入力で永続化し、専用tableは作らない。公開TurnResponseとMechanicalNarrationInputにenemy_reactionsを既定空で追加する。

- [x] 短編v2を新規定義する。入口の依頼確認、広間の探索、記録室/通路の手掛かり、奥の守衛、聖印回収と帰還を含める。会話・探索・選択を増やし、失敗でも警戒/代償つきで進める。v1はそのまま登録して再開を保証する。
- [x] Sceneに型付きcombat定義（enemy_ref、開始flag、敗北Ending、登録済みdamage式）を追加する。プレイヤー攻撃で戦闘開始し、戦闘中の適用済みMechanical Turn後に生存NPCが1回だけ反撃する。会話・未適用Turnでは反撃しない。勝利/撤退/交渉等でSceneが終了した場合は反撃しない。
- [x] Engineの既存攻撃計算を再利用し、先行ActionsのHP/在庫更新後の状態で反撃を計算する。回復後の反撃、HP0敗北、敵HP0時の反撃なし、攻撃miss、撤退、勝利後Scene進行を決定的diceで検証する。
- [x] Application projectionで反撃親ID・NPC/対象・件数・RNG/結果・描写対応を検証し、HP更新と反撃EventとScenario進行を一transactionで保存する。SQLにHP計算を戻さない。
- [x] 実DBで反撃Event失敗時のrollback、同一request/commit再実行、stale epoch、描写retryでもHP/Event増加なし、敗北/撤退後の履歴再開を確認する。プレイヤーActionsと反撃は公開表示も区別する。

## Task 4: 画面・文書・実モデル受入・統合

状態: 実装・検証・レビューとmainへの統合・pushが完了（実装commit 6cc154c）。

担当: UIは契約確定後に独立エージェントへ委任可。controllerが全体検証と文書・実APIを担当。

- [x] 行動候補は登録済みaction_refを送信する専用操作に変更する。自由入力は維持する。未確認要求のexact retryと古いUI無効化を保ち、反撃を別表示する。旧保存済みTurnの互換性を維持する。
- [x] .envのGeminiキーをsecretのまま用い、別DBで自由入力→探索/会話→判定→遷移→結末を実モデルで検証する。固定候補の非再解釈も確認し、呼出数と失敗種別を記録する。30回上限を超えて続行しない。
- [x] 実PostgreSQL全pytest、Node tests、Ruff、mypy、buildを実行し、実browserで開始・候補・自由入力・HP変化・反撃・reload・Endingを確認する。
- [x] README・contracts・必要なADRをjapanese-tech-writingに沿って更新する。起動設定、モデル選択、LLM料金発生、v1/v2互換、戦闘規則、未検証の20〜30分目標と段階5を明記する。
- [x] task reviewと最終全体reviewの必要修正を終え、mainへ統合・pushしてremote SHAを照合する。秘密漏洩と無関係な差分がないことを確認する。検証専用processを停止し記録を保存する。

## 進捗・検証記録

- 2026-09-22（段階4着手）: cleanなmain/origin/main=31db398を確認。直前の全Python572件・JS47件の同一SHAを基準とし、変更前の重複全体検証は省略する。Dockerの既存テスト用PostgreSQLは稼働中。今回の実Gemini受入は新たに最大30要求とし、前回の19要求とは別に記録する。

- 2026-09-22: main/origin/mainのf45d23e一致とcleanを確認。既存Docker PostgreSQL稼働。native worktree toolはタスクcwdがGit repo外のため失敗し、対象repo内のgit worktreeを使用。
- .envはGEMINI_API_KEYの存在だけ確認。公式models/gemini-3.5-flashのGETは200、generateContent対応を確認。推論requestはまだ0回。
- 前ターンの同一baseでは全Python510/JS47を確認済み。今回の変更後に全体検証を行う。
- 2026-09-22: README、docs全22ファイル、既存.agent計画、元のリファクタリングprompt、関連git履歴を再確認。初期770835dではPydanticAIが採用未確定として記載され、8970ac0でその記述がADR参照へ置き換わり、ADR-0006にOpenAI初期adapterが記録されていた。現行依存とsrcにはPydantic AIの導入がない。ユーザーの明示指示を受け、接続差替えだけでなく用途別AgentによるAIオーケストレーションへTask 1を修正した。実装コード変更、サブエージェント起動、commit、pushはまだ行っていない。
- 2026-09-22（実装後）: 用途別port、3用途Agent、モデル設定、SDK client寿命、Fake、worker注入を実装。旧openai.pyとstructured.pyを削除。認可・version・deadlineの旧test double 4種類も同じFake portへ移行し、既存assertionを維持した。
- RED/GREEN: 未実装portのimport failureからAgent実装を開始し、FunctionModel 9件を確認。providerの壊れたHTTP 200応答は3件の再現失敗を先に確認して分類を追加し、Agent/HTTP境界37件が通過。設定/runtime担当はRED12/26件の失敗から実装し、関連74件が通過。
- 初回全pytestは564 passed / 4 failed（502.11秒）。4件はいずれも旧test doubleが新portを持たないことが原因。接続修正後、認可・version・deadlineの該当5件が通過。最終全体suiteは実PostgreSQLを含む572 passed（490.06秒）。
- 実Gemini検証: 専用DB ai_rpg_pydantic_smoke、モデルgoogle:gemini-3.5-flash、秘密値は非表示。初回8要求では交渉が未適用、次の4要求では@ref{hero}記法がgroundingで拒否された。@heroの記法とskill_check.target_ref=nullをpromptに明記した後、会話→入場→探索→交渉→costly_successまで全4Turnが成功。修正後7物理要求=DB予約7、fallbackなし。exact replayとworker再実行による追加要求0、fresh clientで履歴4件と結末を復元。累計19/30要求で終了し、失敗した試行も成功数へ含めていない。
- 実モデル検証の制限: OpenAIは実キーで未実行（実SDK+HTTP mockで確認）。Google SDKはAFC利用に関する警告を一度出すが、tool/function mapを渡しておらず、要求増加は観測されなかった。ブラウザの見た目、20〜30分の所要時間、敵反撃は今回検証していない。
- 静的検査: Ruff通過、mypy 63ファイル通過、JS 47件通過、sdist/wheel build通過。
- 独立レビュー: 仕様適合・品質とも承認。Critical/Importantおよび対応が必要なMinor指摘なし。DB予約順序、SDK例外の秘匿、壊れたHTTP 200で1物理要求に留まることを追加確認。Engine、DB schema、認証、SSEの責務は変更していない。

### 2026-09-22 段階4の実装・受入

- 新規短編v2は6 Scene・17行動・4 Ending。v1を変更せず、新規catalogだけをv2へ更新。登録行動を直接実行し、解釈LLMの呼出を省く。公開Contextに行動型・本人のHP/所持品・公開NPC設定を追加。
- 敵の反撃をEngineで解決し、型付き結果・HP・Event・Scenario進行をatomic保存。攻撃miss、回復後の反撃、撃破時の反撃なし、敗北、撤退、描写repair、stale epoch、commit再送、Event失敗rollbackを実DBで確認。
- 最初の全pytestは683 passed / 1 failed（公開Contextの期待値が旧形式）。新項目を明示した期待値へ更新し、非公開情報を出さないassertionは維持。追加ガードを含む最終全体suiteは689 passed、skipなし（543.95秒）。Node55 passed、Ruff、mypy65ファイル、sdist/wheel build通過。
- Gemini 3.5 Flashを専用ai_rpg_stage4_smokeで確認。最初のharnessは通常会話1要求後、出力集計のDTOキー誤りで停止。修正後、全13Turnの自由入力で回収成功へ到達し、25物理要求=DB予約25、fallbackなし、exact replay/worker再実行の追加呼出0、fresh clientで履歴13件を復元。
- 実測で回復上限前の量を実回復量と語る問題を発見。Engineの公開事実を実際のHP差分へ修正し、Narrator指示を補足。RED/GREENを確認後、実GeminiでHP6→10を4回復と描写し、登録済み直接撤退まで3要求で確認。累計29/30要求で終了。キーは表示・保存せず、通常履歴DBとは分離した。
- 実ブラウザと専用ai_rpg_stage4_browserで、開始、候補直接送信、下書き保持、探索、敵HP10→4、反撃で本人HP10→9、戦闘中reload、自由入力回復9→10と在庫2→1、撃破、回収・帰還、完了後reloadと入力停止を確認。console警告/エラーなし。実ブラウザの敗北・狭幅表示は未確認（敗北は実DBとNodeで確認）。
- シナリオ・反撃projection・UIの個別レビュー、最終統合レビューはいずれも仕様/品質PASS、要対応指摘なし。20〜30分の所要時間・自然言語全体の意味整合性・楽しさは未評価。ブラウザログインと少人数テストは段階5に残す。
- mainを6cc154c801e366997f7fed6ea539247973082218へfast-forwardし、uv sync --frozenと統合後の重点29 testsを確認。origin/mainへのpush後、git ls-remoteで同じSHAを確認した。契約文書のPython例は実行可能、差分の秘密値パターン検査は0件。検証用API/Fake workerを停止し、ユーザーの既存DB・Dockerコンテナは維持した。
