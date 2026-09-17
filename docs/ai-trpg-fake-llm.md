# Fake LLMによる技能判定の一往復を完成させる実装方針

作成日: 2026-09-15  
状態: 主要実装と結合検証を完了。独立processの開発用runnerは後続
対象: caprice1026-disc/AI_RPG

## 1 目的と完成条件

単一プレイヤー・単一PC・進行中Scene一つの構成で、Fake LLMを使って次の一往復を完成させる。

> プレイヤー入力 → 意図抽出 → 技能判定 → 確定保存 → 結果描写 → 次の入力受付

正常系だけでなく、通信再送、描写timeout、worker再起動でも、確定結果の二重適用や処理の取りこぼしが起きないことを確認する。描写完了またはfallbackまで次の入力を待つため、障害時に処理を終端化して入力を再開できることも完成条件に含む。

本書は実装を依頼するときの計画であり、コード変更やテストの完了報告ではない。既存コードについての基準は、直前のレビュー対象コミット[77a0193](https://github.com/caprice1026-disc/AI_RPG/commit/77a0193f1baf1c1160de812a597dec38cbbe10e5)とする。実装開始時には最新差分と既存計画を確認し、すでに修正済みの作業を重複実装しない。

### 実装進捗（2026-09-17）

- Step 1: Domain Command／Result／StateChangeの正本化、Application projection、回復・在庫消費の型付き保存を完了。
- Step 2: Infrastructure限定ORM mapping、描写終端までの受付制約、DB永続LLM予算、phase別attempt／deadline、独立した描写lease／epoch、GMNarrationGeneratedのatomic保存を完了。
- Step 3: 認証済みprincipalを受けるAcceptTurn／GetTurn API、Campaign lock下のsnapshot、公開許可リスト型Contextを実装。workerのLLM呼出前と確定transaction内でもmembership／Actor操作権を再検証し、内部failure codeは明示的な公開code mappingを通す。
- Step 4〜5: 決定的Router、永続予約を通るFake transport、技能判定worker、独立描写worker、Narrative通常応答とMechanical昇格を実装。難易度名からDCへの変換はworkerではなく`mvp_v1` rulesetが所有し、行動不能等のルール拒否は再試行せず`not_applied`へ終端化する。技能判定成功時だけ登録済み公開事実を結果描写Contextへ追加する。
- Step 6: stale epoch、timeout、Schema不正、予算・deadline到達、commit応答喪失、状態version競合、`not_applied`直後の停止を回収する経路とテストを実装。受付時versionをworkerまで固定し、lock待ち中に切れたleaseも`clock_timestamp()`で拒否する。
- Step 7: API受付から次Turnまでの実PostgreSQL受入テストと、APIキー不要のpytest harness再現手順をREADMEへ追加。2026-09-17に実PostgreSQL込み全171件、Ruff、mypy、sdist／wheel buildを確認した。独立したAPI／worker processのfixture投入・停止・再起動CLIは未実装で、後続範囲として明記する。

この進捗欄を現在地の正本とし、以下の計画本文は完成条件として維持する。

## 2 合意済みの前提

| 項目 | 採用方針 |
| --- | --- |
| ゲーム上の真実 | DBに保存したCanonical State |
| LLM | 意図と目的の提案、確定結果の描写を担当。数値結果や状態変更を確定しない |
| Game Engine | ルール、乱数、難易度・補正に基づく結果を決定 |
| 状態変更 | Applicationを経由し、ゲーム結果・Action・Event・Turnをatomicに保存 |
| Action数 | 0〜Nの順序付きリスト。MVP既定上限3は暫定。ルール上の可否は別途検証 |
| 呼び出し予算 | Narrative最大1回。Mechanical通常2回、修復・再描写を含め最大3回 |
| 入力受付 | 解決と描写の両方が終端になるまで、同Campaignの次の新規Turnを受け付けない |
| 再送 | 同じrequest_idと同じ入力は既存Turnを返す。内容が違えば競合 |
| 再起動 | DBから未完了処理を回収。ゲーム確定後は描写だけを再開 |
| 内部retry | LLM予算とは別に試行回数と経過時間を制限し、DBに保持 |
| ルール | 登録されたScene条件とrulesetからDC・補正を決定。LLMが生成した値は不採用 |
| 公開情報 | 公開DTOへ変換して返す。内部Context・秘密・乱数内部情報を直接公開しない |
| ORM | Infrastructure内に型付きモデルを置き、通常のDB操作と行変換を整理する |
| 進捗 | 合意済み・実装済み・結合テストで確認済みを区別する |

Narrativeの創作は音、表情、雰囲気などの演出に限定する。取得可能アイテム、NPC、通路、クエスト達成などの永続的な事実は、登録済み情報または検証済みの採用処理に基づく。NPCの発言を世界の客観的事実として自動採用しない。

## 3 今回の範囲

### 完成させる範囲

- 型と責務の整理、および必要なORMモデルとmigration。
- テスト用のCampaign・PC・Scene・技能判定条件を用意するfixture。
- 認証済みprincipalを受けるTurn受付APIと、認可付きの状態取得API。
- Fake LLMによる技能判定Intentと結果描写。
- Applicationでの参照解決、操作権限・技能・判定条件の検証。
- 固定乱数を注入した技能判定Engine。
- Turn解決と描写の独立したジョブ所有権、永続予算、再開処理。
- 確定結果と描写のイベント保存、公開DTO生成。
- 同じ入力の再送、次の入力の拒否と受付再開。
- 実PostgreSQLを使った結合テストと再現可能な起動手順。

### 今回は完成条件に含めない範囲

実モデル接続、攻撃・回復・アイテム消費の実動作、複数プレイヤー、Directorの高度化、NPC記憶、Quest、RAG、条件分岐付き複合行動、Undo・分岐・完全Event Sourcing、システム雷の演出、本格フロントエンドは後続とする。

最初の受入シナリオは1 Actionとする。Actionリストと上限検証の契約は維持し、型整理を単一Action専用に戻さない。最初の可視化はAPIレスポンスのポーリングでよく、SSE配信の完成は別マイルストーンにする。ただしイベント保存は今回実装し、将来の配信元として利用可能にする。

## 4 既存実装からの変更点

以下は直前レビュー時点の課題であり、実装開始時に解消済みか再確認する。

| 対象 | 変更方針 |
| --- | --- |
| Postgres RepositoryにSQLと検証が集中 | 型付きORMモデル、Applicationの結果検証、保存処理へ責務を分ける |
| 同名Command／Resultの二系統 | Domainの正本を一つにし、外部契約・保存DTOには区別できる名前を付ける |
| CommitBundleの汎用Mappingと位置依存tuple | 型付きResolutionResultから保存内容を導出する |
| メモリ内CallBudget | 送信前のDB予約へ変更。workerの引継ぎでも残数を保持 |
| 解決epochを流用した描写保存 | 描写に独立したleaseとepochを持たせる |
| 描写保存時にイベントがない | 文章・Choice・GMNarrationGeneratedを同じtransactionで保存 |
| SELECTごとに変わり得るsnapshot | 短い一貫した読取transactionで取得 |
| 解決済みなら次のTurnを受付可能 | 描写の終端まで入力受付を閉じる |
| ゲーム更新がDamageだけに依存 | 技能判定は状態変化ゼロを許可。後続のHealing／ItemConsumedを追加できる型境界にする |

既存の再送・競合・認可・rollbackテストを保護し、ORM移行で期待する振る舞いを変えない。意図的に変更する「描写待ち中の次Turn受付」については、旧テストの期待値とADRを新方針に合わせて更新する。

## 5 型と責務

### 正本となる型

| 型 | 生成元 | 内容 |
| --- | --- | --- |
| PlayerTurnInput | Client | request_id、expected_state_version、actor_id、textまたはchoice_id |
| SkillCheckIntent | Fake LLM | 技能参照、目的、対象参照。DC・補正・結果は含めない |
| SkillCheckCommand | Application | actor、登録済み判定条件、技能、解決済みDC・補正、行動識別情報 |
| ResolutionResult | Engine | 順序付きAction結果、ダイス記録、型付き状態変化 |
| ActionRecord／EventRecord | Mapper | ResolutionResultとTurnメタデータから導出した保存用DTO |
| NarrationInput | Application | 確定時点の公開結果、許可済みScene情報、描写制約 |
| NarrationDraft | Fake LLM | 描写とChoice案 |
| TurnResponse | Application | 公開可能な状態、文章、Choice、Action結果、復旧情報 |

Domainの型はORMとLLM SDKへ依存しない。Pydanticによる外部入力の構造検証と、ゲームルールの検証を分ける。型変換が必要な場合は一つの明示的なMapperに集約する。

ゲーム結果の内容と、同じ内容を表すEventを呼出側が独立に組み立てない。ResolutionResultからEventを導出し、Canonical更新も同じ型付き状態変化に基づく。EventのID・Campaign内sequence・確定versionは保存側が付与する。

今回の技能判定はHP等を変更しないため、状態変化リストは空になる。ActionとEventは保存するが、state_versionは増やさない。技能判定で秘密を発見しフラグを更新する機能は、この最初のシナリオには含めない。

### Applicationの主なユースケース

メソッド名は実装案であり、既存の命名へ適合させてよい。

- AcceptTurn: 認可、再送判定、受付可否、入力保存。
- ResolveTurn: 一貫した状態取得、Intent抽出、ルール検証、Engine呼出、atomicな確定。
- GenerateNarration: 保存済み公開スナップショットから描写し、条件付き保存。
- FinalizeTurnFailure: 未確定処理を失敗として終端化、または確定済み処理にfallbackを保存。
- GetTurn: 認可付きの公開DTO読取。

workerはこれらを呼び出す実行役とし、ルールやSQLを持たせない。Repositoryは認可に必要な情報の取得と保存を担い、Applicationがポリシーを判断する。既存のDB制約による最終防御は維持する。

## 6 ORMと永続化

SQLAlchemyの型付きモデルはInfrastructureに閉じ込める。Campaign、Scene、Turn、Action、Event、Choiceと必要なCanonicalテーブルのmappingを整え、Sessionを外へ返さない。

通常のCRUDはORMまたは型付きクエリ式を使う。lease取得、条件付き更新、連番確保、一括INSERTは明示的なSQLAlchemy式で書く。DB関数や追記専用トリガーはAlembic内のSQLとして残してよい。全SQL排除や汎用Repositoryフレームワークの作成は目標にしない。

Action挿入時に親Turnのcommittedを要求する既存トリガーを維持する場合、ORMの暗黙flush順に依存しない。Turnの確定UPDATEを明示的にflushし、その後Action／Eventを保存し、すべてを一つのtransactionでcommitする。

既存migrationを適用済みの環境がある前提で、機能変更は追加revisionにする。Base.metadataとAlembicを接続しても、トリガー、複合制約、部分インデックスの差分を自動生成だけに任せない。

### 追加または変更する永続情報

| 情報 | 用途 |
| --- | --- |
| 解決用lease／epoch | 解決workerの取得、更新、期限切れ回収、古い所有者の拒否 |
| 描写用lease／epoch | 解決処理と独立した描写workerの所有権 |
| llm_call_countと有効予算 | Intent・repair・描写で共有するTurn単位の永続予算 |
| phaseごとのattempt_count | 内部処理の試行回数上限 |
| phaseごとの初回開始時刻・deadline | 再起動でも延長されない実行期限 |
| next_attempt_at・内部failure_code | 回収可能なretryと原因記録。公開理由と分離 |
| narration_input | commit時点の公開スナップショット |
| 型付きScene判定条件 | skill、DC区分、対象等を登録情報として解決 |

解決・描写ごとの列をTurnに置く方式を、この範囲では第一候補とする。既存コードで専用jobテーブルが導入済みなら同等の契約を使い、二重に所有権を管理しない。

### 入力受付を閉じる条件

旧条件の「resolutionがpending／resolving」だけでは足りない。以下のどちらかに該当するTurnがあれば、同Campaignの新しいrequest_idを拒否する。

- resolution_statusがpendingまたはresolving。
- narration_statusがpendingまたはgenerating。

この条件をApplicationのチェックと、Campaign内一意性を守るDB制約に反映する。resolutionがfailed／not_appliedでも描写が未完了なら受付は閉じたままなので、必ず説明文またはfallbackを保存して終端化する。

同じrequest_idの再送とGetTurnは、この受付制限の対象外。既存Turnを返す。会話だけではstate_versionが変わらないため、Choiceはversion一致だけでなく無効化状態と提示元も確認する。

### 一貫した状態の取得

MVPでは、短いtransaction内でCampaignを先にロックし、version・PC・技能補正・Scene判定条件をまとめて読む方式を推奨する。Canonical更新側も同じCampaignロック規約を守る。

取得後はtransactionを終了し、LLM待ち・Engine計算中はロックを保持しない。確定時にversionと所有権を再検証する。型付きORMに変えても、この規約を省略しない。

## 7 状態遷移と受付再開

| 場面 | resolution_status | narration_status | 次の新規入力 |
| --- | --- | --- | --- |
| 受付済み | pending | pending | 不可 |
| 解決中 | resolving | pending | 不可 |
| 判定を保存済み | committed | pending | 不可 |
| 結果描写中 | committed | generating | 不可 |
| 通常完了 | committed | completed | 可 |
| 確定後の描写fallback | committed | fallback | 可 |
| 確認質問・ルール上不可能 | not_applied | completed | 可 |
| 未確定の技術的失敗を説明済み | failed | fallback | 可 |

技能判定のfailureは正常なゲーム結果なのでcommittedになる。Narrativeの通常応答も0 Actionsのcommittedとして扱う。入力を勝手に別の行動へ変更して成功させない。

失敗を確定する処理でもCampaign、Turnの順でロックし、epochを検証する。ゲームがすでにcommittedなら、resolutionをfailedに戻さず描写fallbackへ進む。

## 8 Fake LLMと受入シナリオ

### Fakeの置き場所

既存の構造化出力Adapterの下にFake transportを注入する。出力Schema、入力データ、呼出用途をtransportへ渡せる境界を作る。FakeだけがPydantic検証やDB予算予約を迂回する構成にしない。

Fakeは自然言語理解の模倣ではなく、シナリオごとに返す応答を指定できるものにする。最小限の動作モードは正常応答、無効な構造化出力、timeout、制御された一時障害。解決用と描写用の応答を区別する。

Fake LLMの応答順と、ダイスの乱数源は別々に制御する。モデルが判定結果を決めたように見えるfixtureを作らない。固定seedまたは所定の出目を返す乱数源をEngineへ注入し、FakeはEngineの確定結果を受けて描写する。

### 初期fixtureの例

| 項目 | テスト値 |
| --- | --- |
| Campaign／PC | テスト用に明示的に作成した単一Campaignと操作可能なPC |
| Scene | 練習場。観察判定の登録条件を一つ置く |
| 入力 | 「周囲を注意深く観察する」 |
| Fake Intent | skill_check、perception、登録済みの観察目的 |
| DC | Sceneのnormalに対応する12 |
| 技能補正 | PCのCanonicalに保存した+2 |
| テストの出目 | 10 |
| Engine結果 | 10 + 2 = 12、success |
| 公開する事実 | 判定成功と登録済みのScene描写。新しいアイテムや秘密フラグは生成しない |

この数値は受入テストの例であり、すべてのプレイヤーやSceneの固定値ではない。failureのfixtureも別に用意する。

### 正常経路

1. テストprincipalを認証境界から注入し、CampaignとActorの権限を確認する。
2. 新しいrequest_idで入力を受け付け、pending Turnを保存して応答する。
3. 解決workerがleaseを取得し、一貫したCanonical snapshotを取得する。
4. ルールベースRouterが明示的な技能判定をMechanicalへ送る。
5. DBでLLM予算を1回予約し、FakeからSkillCheckIntentを受け取る。
6. Applicationが登録済み技能・Scene条件・Actorを検証し、Commandを生成する。
7. EngineがDC・補正と乱数から結果を計算する。
8. Action、DiceRolled、ActionResolved、Turnの確定状態、narration_inputを同時保存する。Canonical変更がないのでstate_versionは維持する。
9. 描写workerが別のleaseを取得し、LLM予算を1回予約する。
10. 保存済みnarration_inputを使ってFakeが描写を返す。
11. 文章、Choice、GMNarrationGenerated、描写終端状態を同時保存する。
12. GetTurnが公開結果を返す。次のrequest_idで新しい入力を受け付けられる。

NarrativeからMechanicalへの昇格では、ResolutionRequiredを返した最初の呼出をIntent抽出として数える。独立した分類LLMを追加しない。通常のNarrative、確認質問、昇格は小さなFakeシナリオで契約を確認し、中心の受入経路は上記の技能判定とする。

## 9 予算と障害復旧

### LLM予算

呼出の直前に短いtransactionで、Turnの有効予算、現在のphase、lease所有権と期限を確認して1回分を予約し、commitしてからtransportを呼ぶ。予約UPDATEのtransactionをLLM待ちまで保持しない。

timeout、拒否、不正出力、repairも消費する。再起動時にカウンターをリセットしない。予約後に送信前停止した場合も、MVPでは消費済みとして扱い、上限を超えて補填しない。

### 内部retry

LLM予算とは別に、phaseごとの試行回数とdeadlineをDBに保存する。再取得は回数を増やすがdeadlineを延長しない。上限値は設定化し、実装時のテストで初期値を決めて記録する。未設定のまま無制限になるデフォルトは用意しない。

LLM単発timeout、lease期間、lease更新間隔、phase全体deadlineの関係を設定検証する。既存ADRの初期値を基点とし、変更した実効値をテストと運用ログへ残す。

| 障害 | 方針 |
| --- | --- |
| 曖昧な意図 | 確認質問、not_applied。自動で別行動に置換しない |
| 未登録技能・ルール上不可能 | 説明してnot_applied。機械的な別行動へ修復しない |
| Mechanical出力のSchema不正 | 残予算と期限内でrepair。使い切ったら未確定失敗を終端化 |
| 状態バージョン競合 | 古い計算結果を破棄し、状況更新を案内。黙って新状態で再ロールしない |
| Engineの想定外例外 | ゲーム未適用のまま技術的失敗として記録 |
| ゲーム確定transactionの例外 | 全体rollback。再試行可能か分類し、上限付きで処理 |
| commitの応答喪失 | DBで確定状態を再照会してから判断。未確定と決めつけない |
| ゲーム確定後の描写timeout | 保存結果から予算内で再描写。残予算がなければテンプレートfallback |
| worker停止 | lease期限切れ後に別workerが回収。古いepochの保存を拒否 |
| 内部試行回数・deadline到達 | 未確定なら失敗説明、確定済みなら描写fallbackで終端化 |

DB自体が停止している間は、安全な確定確認や入力受付再開はできない。利用不能を返し、DB復旧後に回収して終端化する。「障害時に必ず即時再開できる」とは扱わない。

leaseの取得・期限判定はDBの時刻を基準にそろえる。描写workerの所有権も保存直前に確認し、単なる先着保存だけで古いworkerを排除したことにしない。

確定前の乱数結果は公開しない。保存済みのAction結果を再送や再描写で再ロールしない。未確定の計算結果を必ずクラッシュ前と同じ出目に復元する要件は今回設けず、固定乱数によるテスト再現性と、確定済み結果の不変性を保証する。

## 10 公開DTOとNarrativeの制限

公開DTOは許可リスト方式で生成する。許可するのはTurn ID、公開状態、公開可能なダイス・判定結果、文章、Choice、簡潔な復旧案内など。DBのJSONをそのまま返さない。

以下はClientに返さない。

- 内部Context、system instruction、GMの秘密情報。
- 未公開NPC情報、内部用の判定条件。
- 乱数seed、内部draw情報、worker所有トークン。
- stack trace、DBエラー、providerの生レスポンス。

既存TurnResponseのRecoveryReasonは内部ログ用分類と公開用コードの対応を定義する。新たな内部failure_codeをそのまま表示しない。確定結果の数値はEngineから生成した公開DTOを表示の正本とし、描写文の数値を解析して状態を更新しない。

Fakeシナリオでは登録済み情報だけを描写に含める。これは将来の実LLMに対する完全な事実検証の完成を意味しない。実モデル接続前に、描写が未登録のアイテム等を約束しない評価を追加する。

## 11 実装順序と確認条件

### Step 0 既存成果と進捗を確認する

- 最新コード、AGENTS指示、関連ADR、既存ExecPlan、migration適用状況を確認する。
- 本書の合意内容と矛盾するADRを更新する。特に描写終端までの受付制限を明記する。
- 既存の関連ExecPlanがある場合はそこへ統合し、同じ計画を別形式で重複管理しない。
- 既存テストを実行し、成功・失敗・skip・環境未整備を区別して記録する。

確認条件: 現在地と未完了部分が明示され、過去のレビュー結果だけを最新状態として扱っていない。

### Step 1 型とゲーム結果の正本を整理する

- Domain Command／ResolutionResultを整理し、同名の異なる型を解消する。
- 外部Intent、Domain、保存DTO、公開DTOの変換位置を固定する。
- 技能判定のAction結果からイベントを一意に導出する。
- 未登録技能、Actor不一致、行動不能などをEngine／Applicationで拒否する。

確認条件: DB・LLMなしで技能判定でき、結果を保存契約と公開DTOへ変換できる。

### Step 2 ORMとDB制約を整備する

- 通常のDB操作をORM／型付き式へ移行し、Repositoryを責務ごとに分ける。
- 描写待ちを含むCampaignの受付制約を追加する。
- 独立した描写所有権、永続予算、試行回数・deadlineに必要なmigrationを追加する。
- 既存の複合FK、追記専用制約、Idempotencyを維持する。

確認条件: 空DBからheadへ適用でき、既存データを持つ前revisionからも移行できる。既存の並行受付・rollbackテストが通る。

### Step 3 受付と一貫したContext取得を接続する

- テストfixtureと認証principalの注入境界を用意する。
- AcceptTurn、GetTurn、Choice検証をAPIへ接続する。
- snapshotの一貫性、最小Context、公開範囲のフィルタを実装する。
- 同一requestの再送を、新規受付制限や現在versionの拒否より先に判定する。

確認条件: 再送は既存Turn、新しい入力は処理完了まで競合、認可違反は拒否となる。

### Step 4 解決workerとFake Intentを接続する

- pending／期限切れresolvingの発見、取得、lease更新、回収を実装する。
- DB予算予約を通してFake Intentを取得する。
- ApplicationがDC・補正を登録情報から解決する。
- Engine結果をatomicに保存し、描写ジョブとして回収可能な状態を作る。

確認条件: LLM通常1回でAction・DiceRolled・ActionResolvedが保存され、ゲーム確定後の再実行で増えない。

### Step 5 描写workerと受付再開を接続する

- 解決とは独立した描写取得・lease・epochを実装する。
- 保存済み公開スナップショットからFake描写を取得する。
- 文章・Choice・GMNarrationGenerated・描写終端を同時保存する。
- 予算切れやtimeoutのテンプレートfallbackを実装する。

確認条件: 通常2回のFake呼出で完了し、次の入力を受け付けられる。描写失敗でもゲーム結果を保持する。

### Step 6 障害復旧を接続する

- 解決前・確定直後・描写保存直前の停止から回収できるようにする。
- 不明なcommit結果の再照会、古いepochの拒否、期限到達時の終端化を実装する。
- Fake障害と制御可能な時計を使い、長時間sleepに頼らず検証する。

確認条件: 再起動しても予算やdeadlineがリセットされず、確定済みゲーム処理を再実行しない。

### Step 7 一往復の受入テストと起動手順を残す

- 実PostgreSQL、実Repository、Application、Engine、worker、Fake transportを接続する。
- API受付から終端DTO取得、次Turn受付までを自動検証する。
- APIとworkerの起動、fixture投入、サンプル入力、結果確認、停止・再起動の手順を記載する。
- 認証のテスト用差し替えが通常起動で有効にならないことを確認する。

確認条件: 別の開発者が手順に従って、実モデルのAPIキーなしで同じ一往復を再現できる。

## 12 結合テストの必須ケース

| ID | ケース | 確認する結果 |
| --- | --- | --- |
| T01 | 技能判定success | 指定出目と補正から正しい判定。Actionと関連Eventが保存される |
| T02 | 技能判定failure | 正常なcommitted。システム失敗にしない |
| T03 | 同一requestの再送・並行送信 | Turnは一つ。追加のLLM呼出、Action、乱数適用なし |
| T04 | 同一request_idで異なる本文 | 競合。既存結果は変わらない |
| T05 | 描写pending／generating中の新規入力 | 拒否。完了／fallback後に受付可能 |
| T06 | 不正Actor、別Campaign参照、無効Choice | 認可または制約で拒否 |
| T07 | IntentにDC・damage・state_mutation等が混入 | Schemaで拒否。Canonical未変更 |
| T08 | 未登録技能・曖昧な行動・不可能な行動 | 確認または説明。別行動へ置換しない |
| T09 | 出力Schema不正とrepair | 物理呼出分を消費。上限超過前に停止 |
| T10 | Event INSERT失敗 | Action・Turn確定・version等もrollback |
| T11 | 確定後の描写timeout | 結果を保持し、予算内retryまたはfallback |
| T12 | worker再起動 | phaseに応じて回収。確定済みActionを再実行しない |
| T13 | 古い解決worker／描写workerの保存 | それぞれのepoch不一致で拒否 |
| T14 | 呼出予約後の停止 | 再起動でも予算が復活しない |
| T15 | commit応答喪失 | DBで状態確認し、確定済みなら再実行しない |
| T16 | retry上限・deadline到達 | 終端化。DB復旧後は入力受付を再開できる |
| T17 | snapshot取得中の並行Canonical更新 | 一貫した状態を取得するか、安全に競合として拒否 |
| T18 | 公開DTO | 内部Context、秘密、乱数内部情報、stack traceを含まない |
| T19 | 予算1のNarrativeとMechanicalへの昇格 | 不要な分類呼出を加えず、Turn全体の予算を守る |
| T20 | 描写保存とイベント保存の片方で失敗 | 両方rollbackし、再取得後も二重記録しない |

テストは実装手順やSQL文字列そのものより、保存結果・公開結果・予算・競合・rollbackを確認する。実DBテストは隔離した専用DBで行い、共有環境や本番データを使用しない。DBテストが環境変数未設定でskipされた実行を「結合テスト確認済み」として扱わない。

## 13 三段階の進捗管理

各機能について、仕様への合意、実装、結合検証を別々に記録する。完了条件を満たしていない列を、計画書やADRの存在だけで完了にしない。

下表の実装・検証欄は今回の計画に対する完了宣言ではない。開始時に現行コードと実行結果から更新する。

| 機能 | 合意済み | 実装済み | 結合テストで確認済み | 証跡 |
| --- | --- | --- | --- | --- |
| Command／Resultの正本と変換 | ✓ | ✓ | ✓ | Domain型、projection unit／PostgreSQL test |
| ORMとDB制約の維持 | ✓ | ✓ | ✓ | `0003`〜`0005`、metadata／migration test |
| 描写終端までの入力制限 | ✓ | ✓ | ✓ | 並行受付、描写pending／fallback後の受付test |
| 再送・認可・Choice | ✓ | ✓ | ✓ | API／PostgreSQLの再送・権限・Choice、worker再認可test |
| 一貫したsnapshot | ✓ | ✓ | ✓ | Campaign lockと並行更新test |
| 登録済みDC・補正での技能判定 | ✓ | ✓ | ✓ | Fake success／failure、未登録・曖昧・行動不能test |
| 永続LLM予算 | ✓ | ✓ | ✓ | timeout・invalid・Narrative昇格・worker交代test |
| ゲーム結果のatomicな保存 | ✓ | ✓ | ✓ | rollback、重複確定、commit応答喪失test |
| 独立した描写所有権 | ✓ | ✓ | ✓ | stale narration epoch、timeout、event rollback test |
| retry上限と終端化 | ✓ | ✓ | ✓ | attempt／deadline exhausted cleanup test |
| 公開DTOとNarrative制限 | ✓ | ✓ | ✓ | API許可リスト、failure code mapping、Fake call用途・Schema・Context test |
| APIから次Turnまでの一往復 | ✓ | ✓ | ✓ | Fake skill-check受入test、READMEのpytest harness手順 |

「実装済み」には対象commitと主要ファイルを付ける。「結合テストで確認済み」にはテスト名、実行日、対象commit、DB環境、成功／失敗／skipを記録する。既存テストの過去成功は保持するが、新しい合意内容まで確認した証拠にはしない。

## 14 マイルストーンの終了判定

- [x] Fake LLMと実PostgreSQLで、技能判定success／failureの一往復が再現できる。
- [x] 通常のMechanical処理は2回の呼出で完了し、最大3回を再起動後も超えない。
- [x] Action／Eventがatomicに保存され、同一requestで二重適用されない。
- [x] 描写完了またはfallbackまで新規入力を拒否し、その後は受付を再開できる。
- [x] 解決・描写の独立した所有権と期限切れ回収が動く。
- [x] 保存済み結果を再ロールせず、描写だけを復旧できる。
- [x] 内部retry上限と期限がDBに残り、未完了Turnを放置しない回収処理がある。
- [x] APIから内部Contextや秘密情報が漏れない。
- [x] 既存の重要テストと今回の結合テストが通り、skipを成功扱いしていない。
- [ ] 起動・fixture・一往復・再起動の手順と、三段階の進捗証跡が残っている。

完了後は同じ経路に攻撃、HealingApplied、ItemConsumedを追加する。実モデル、SSE、本格UI、Directorの拡張は、それぞれ独立した次のマイルストーンとして進める。
