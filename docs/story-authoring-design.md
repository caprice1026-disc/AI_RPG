# ユーザー作品の作成・公開・プレイ：詳細設計

- 決定日: 2026-10-04
- 状態: ユーザーと合意した実装予定の設計。機能が実装済みであることを意味しない。
- 調査基点: `98eced1293af393952ef1a0f390d90e07e9da3a0`
- 対応する[実装方針書](story-authoring-implementation.md)
- 対象: 単人数の短編を、作者がコードを書かず作成・試遊・公開し、別ユーザーが遊べるようにする。

## 1. 背景、合意、実装の意図

現状は短編のプレイ、保存・再開、OIDC認証、型付きScenario、分岐・結末がある。次の目標は「作者が作品を作る → 試遊する → 公開する → 別ユーザーが遊ぶ」を一巡させること。2026-10-04の会話で以下を合意した。

| 合意事項 | 意図・理由 |
| --- | --- |
| 任意作品で動く実行基盤を最優先にする | 投稿画面だけ作っても、敵・設定・読み込みが固定なら別作品を遊べない |
| 作品・公開版・プレイ状態を分離する | 作者の修正でプレイ中のストーリーを突然変えない |
| 作成画面はテンプレート＋フォーム＋試遊 | 最初から巨大なノードエディタを作らず、非技術者にも作れる導線を用意する |
| 未完成の下書きを自動保存する | 作成途中の不整合を理由に入力を失わせない |
| フォームからLLMが整合性を確認し、設定や展開を補完する | 原稿を遊べる定義へ変換する負担を減らす。ただし作者の意図を上書きしない |
| AIによる自然文からの作品作成も行う | 優先度は実行・保存・手動作成基盤より低い |
| 後から拡張できる境界を設ける | テンプレートや作品数の増加で、本体に作品別分岐を増やさない |
| 一般公開機能も実装する | 限定公開で一巡させてから、一覧・登録・運営機能へ広げる |

「任意作品」は現行ルールで表現できる作品を意味する。あらゆるジャンルのゲーム機構を無制限に実行する意味ではない。LLMは意図解釈と提案・描写を担当し、Applicationが検証し、Game Engineが判定と数値計算を行う現行境界を維持する。

## 2. 現状の対応箇所と不足

- `src/ai_rpg/scenarios/models.py`: ScenarioDefinition、Scene、Action、Flag、Ending、BoundedWorldおよび参照検証がある。
- `src/ai_rpg/scenarios/catalog.py`: 組み込みJSONをメモリ上のカタログに読み込む。
- `src/ai_rpg/application/adventures.py`: 一覧は礼拝堂v3・灯台v1を直接指定し、開始時にもBUILTIN_SCENARIOSを参照する。
- `src/ai_rpg/infrastructure/postgres/adventures.py`: ゴブリン、鉄の剣、回復ポーション、敵能力値などが固定。保存済み一覧のタイトルも組み込みカタログから取得する。
- `src/ai_rpg/application/scenarios.py`: 礼拝堂・灯台専用の文章矛盾チェックがある。
- `src/ai_rpg/api/app.py`、`src/ai_rpg/runtime.py`: API・workerのScenario取得経路に組み込み依存がある。
- `frontend/src/App.vue`: プレイ中心の画面で、作品編集・公開導線は未実装。
- `mvp_scenario_runs`: scenario_refとscenario_versionはあるが、ユーザー作品の版を参照するFKはない。

ADR-0012の「定義はリポジトリJSON、DBには実行状態のみ」は固定作品の段階の判断。今回のユーザー編集・公開という要件では定義のDB保存が必要になる。実装時に後継ADRを追加し、過去ADRの履歴を消さず適用範囲を更新する。ADR-0016の自由行動、早期達成、目標放棄、状態確定境界は維持する。

## 3. 初期スコープと拡張境界

初期は1人用の短編、共通の既存ルール、独立した冒険とする。舞台、人物、敵、アイテム、判定条件、分岐、結末を変更可能にする。地点3〜5程度をテンプレートの目安とするが最低通過地点数にはしない。

当初は共同編集、他作者作品のリミックス公開、キャラ・アイテムの作品間持ち越し、連作管理、任意Python/JS、作者定義の任意計算式、リアルタイム戦闘、ゲーム内共通経済を対象外とする。自作品の複製は対応する。

テンプレートの追加とエンジン機能の追加を分離する。未知の機能は黙って無視せず、対応不可として検証エラーにする。初期はモジュール・型・取得インターフェースで境界を作り、汎用プラグイン基盤や別マイクロサービスを先に作らない。

## 4. データの3層

### 4.1 作者原稿（AuthoringDraft）

フォーム、メモ、未定事項、設定の固定指定、テンプレート由来の初期値を持つ。不完全でも保存できる。型・サイズ・権限など保存自体の制約は課すが、未入力・参照切れ・結末未設定は下書き保存を妨げない。

各編集対象には安定IDを持たせ、並び順や表示名と同一視しない。IDはサーバー側で採番・検証し、ユーザーが内部flag_refを手入力しなくても「鍵を入手済み」などの表示から条件を選べる。

### 4.2 AI提案（AuthoringProposal）

元のdraft_revision、生成設定、提案差分、指摘、変更理由、作者の採否を保存する。文章全置換を基本にせず、安定IDまたはフィールドpath単位の変更案を返す。

設定は `fixed`（固定）、`fillable`（補完可能）、`undecided`（作者が未決定のまま保留）を区別する。AIはfixedを変更せず矛盾を報告する。undecidedは選択肢を提案できるが自動確定しない。固定解除は作者の明示編集のみ。採用された提案も新しい下書きリビジョンとして記録し、公開版を変更しない。

### 4.3 実行定義（CompiledScenario）

参照解決済みで、対応ルールと機能が明示された厳密なScenarioDefinition。初期化対象、地点、行動、条件、効果、秘密情報、結末、世界の確定事項を含む。

手動作成でもAI生成でも、同じコンパイル・検証経路を通す。完全に指定された原稿からの変換にはLLMを必須にしない。LLM出力は候補であり、直接公開・実行しない。

## 5. 保存モデル

以下の名称は新規実装の論理名。既存テーブルとの整合を実装時に調整してよいが、責務・制約は維持する。

| テーブル | 主なフィールド | 制約・用途 |
| --- | --- | --- |
| stories | id UUID, owner_principal_id, visibility, lifecycle, current_release_id, created_at, updated_at | 作品の恒久ID。公開版は同一作品のreleaseのみ参照 |
| story_drafts | story_id, revision bigint, authoring_schema_version, payload JSONB, template_id/version, updated_at | 初期は1作品1作業下書き。所有者とrevisionによる比較更新 |
| story_draft_revisions | story_id, revision, payload, actor_id, source, created_at | 自動保存・AI採用・復元の履歴。復元は過去を上書きせず新revision |
| story_versions | id, story_id, kind, release_number, source_draft_revision, schema_version, ruleset_ref/version, capability_requirements, payload, content_hash, created_at | kindはreleaseまたはplaytest。payloadと出典は作成後変更不可 |
| story_validation_reports | id, snapshot/hash, validator_version, errors, warnings, coverage, created_at | 特定内容への検証結果。他の内容の公開根拠に流用しない |
| story_playtest_records | id, story_version_id, campaign_id, actor_id, result, notes | 試遊版・公開候補内容との対応、到達結末、未確認事項 |
| authoring_jobs | id, owner, story_id, base_revision, kind, state, request_id, attempts, lease, usage, result | 後述の非同期ジョブ |
| authoring_proposals | id, job_id, base_revision, changes, findings, decision | 採用済みかと適用先revisionを記録 |
| story_reports / moderation_events | story_id, reporter/admin, reason, action, created_at | 一般公開段階の通報・停止の監査 |

新規runには `story_version_id` を追加し、Campaignの開始時に固定する。既存scenario_ref/versionは移行期の互換参照として保持する。外部参照はUUID、作品内参照は安定したローカルrefを使い、作者間の同名作品を衝突させない。

`(story_id, release_number)` と `(story_id, revision)` は一意。公開版の連番はDB transactionで採番する。payloadの更新・削除は通常アプリ資格情報で禁止し、参照中版はFKで削除制限する。通常の下書き履歴の保持期間は設定可能とし、公開・試遊・実行から参照するスナップショットを履歴整理で消さない。

タイトル、紹介文、タグ、注意事項などの公開メタデータは下書きで編集し、公開時に版へ固定する。一覧は現在公開版のメタデータ、保存済み冒険は開始時の版のメタデータを表示する。公開範囲・停止状態は作品単位の運用属性として別管理する。

## 6. 版、公開、互換性

区別するバージョンは、下書きrevision、作品release_number、authoring/compiled schema version、ruleset version、template version。互いの代用にしない。互換処理を変える場合も、過去の確定状態を暗黙に再計算しない。

作品のvisibilityはprivate / unlisted / public。lifecycleはactive / withdrawn / blocked / archived。下書きが存在することと公開中であることは排他的ではない。

公開手順:

1. 所有者、request_id、expected_revisionを確認する。
2. 対象リビジョンを固定し、コンパイル、検証、試遊記録を照合する。
3. transactionで下書きrevision・停止状態を再確認する。
4. immutableなreleaseを作成し、current_release_idを更新する。
5. 同じrequest_idの再送では同じ結果を返し、別payloadなら競合にする。

検証やLLM呼出の間はDBロックを保持しない。公開時には保存済み検証レポートのhash・validator version・対象revisionを照合し、古い結果を使わない。検証後に原稿が変わった場合は再検証する。

冒険開始時は公開権限・停止状態と指定版をtransaction内で確認し、runと初期状態をatomicに保存する。初期の通常開始は現在公開版のみ。開始画面で版が更新された場合は黙って最新版へ切り替えず競合を返して再表示する。同じ開始request_idの再送は既存runを返すが、権限失効・運営停止は再確認する。

通常の公開停止は新規開始を止め、既存runは固定版で継続する。運営blockedは新規開始・継続を止める。作者の削除操作はarchive扱いとし、参照中の実データを物理削除しない。特権による削除は別の保守手順とする。

LLMの同一文章や乱数列の完全再現は保証しない。開始時の作品内容、ルール、確定イベント・生成事実・描写を保存して継続性を守る。モデル設定と実呼出のモデル情報は診断用に記録し、モデルが提供終了した場合の代替は明示して検証する。互換なreaderがない版を最新版として解釈してはいけない。

## 7. 実行基盤の一般化

### 7.1 初期化データ

CompiledScenarioにNPC/敵、配置、戦闘能力、初期所持品、許可されたアイテム効果、判定難易度を持たせる。共通ルールの範囲・上限を検証し、未知の効果を実行しない。敵がいない作品は敵Entityを作らない。初期装備を既存ルールの共通presetに置く場合も、作品選択の意味と対応範囲を明記する。

schemaにフィールドを追加するだけでなく、生成したEntity参照が攻撃・技能判定・アイテム使用・UI表示まで解決できることを保証する。

### 7.2 世界の整合性

重要物の所在、人物の存在、達成条件など構造化できる事実は型付きデータで保持し、Applicationが確定stateと照合する。秘密・公開済み・作者専用の情報を区別する。文章の意味全体の矛盾は補助LLM検査と回帰例で扱い、完全検出を宣言しない。

旧作品固有の正規表現は旧版互換経路へ隔離し、新規作品のためにscenario_ref分岐を追加しない。旧版の挙動は回帰検証なしで変更しない。

### 7.3 定義取得

ScenarioRepository/Resolverという取得境界を用意し、API・AdventureService・一覧・TurnQuery・解決worker・描写workerが同一story_version_idを読む。DBアクセスは境界で済ませ、進行判定の純粋コンポーネントへ定義を渡す。キャッシュキーはimmutable版ID/hashとし、「最新版」のキャッシュを実行中runへ使わない。

Campaign lock、state_version、worker lease、冪等性、Action/Event/Turnのatomic commit、描写再試行で状態更新を繰り返さない規則を維持する。一般化は自由行動を固定選択肢だけに狭める変更ではない。

## 8. テンプレートと作成UI

TemplateDefinitionはtemplate_id/version、表示名、説明、質問項目、フォームセクション、初期原稿、推奨構成、required_capabilitiesを持つ。初期は管理された同梱テンプレートをregistryで列挙し、追加時に画面へ作品別分岐を増やさない。任意HTML/JSをテンプレートとして実行しない。複雑な項目は許可されたフォーム部品のregistryで拡張する。

テンプレート適用時に原稿へコピーして出典を保存する。後日のテンプレート更新は既存原稿・公開版へ自動反映しない。新規テンプレートと新エンジン機能は別の変更単位とする。

画面はマイ作品、基本情報、世界・目的、場所、人物・アイテム、進行条件、結末、AI裁量、検証・試遊、公開設定で構成する。初期はフォーム中心とし、グラフ表示やノード編集は後回し。公開紹介と秘密設定は入力欄でも区別する。

自動保存:

- 入力停止後のdebounce保存。初期提案値は2秒で、設定・実測で調整する。
- clientはexpected_revisionとrequest_idを送信する。serverは所有者確認後に比較更新し、新revisionと保存時刻を返す。
- 競合は409で返し、入力を保持して再読込・差分確認へ誘導する。last-write-winsにしない。
- 応答喪失後の同一要求再送は同じrevisionを返す。同一request_idで異なる内容は拒否する。
- 保存中・保存済み・保存失敗を表示し、失敗時は入力を保持して再試行する。未保存時の離脱警告を行う。
- 公開・試遊・AI実行は保存完了後のrevisionを明示して開始する。
- 初期は完全オフライン編集を保証しない。端末内に原稿を永続キャッシュする場合はアカウント分離・ログアウト時消去を別途設計する。

## 9. コンパイル、検証、試遊

検証結果はcode、severity、field_path/entity_ref、message、修正案、検証範囲を返す。未実施を成功扱いしない。

| 検証 | 判定方針 |
| --- | --- |
| 型、ID、参照、対応schema/ruleset/capability | 不正なら公開・試遊開始をブロック |
| NPC/敵/アイテム/判定の初期化と実行参照 | 解決不能・範囲外ならブロック |
| 地点到達とフラグ条件 | 小規模では有限状態探索。必ず詰むなど確実な不正はブロック |
| 探索上限超過、自由行動の未評価経路 | 未検証の警告として返す。証明済みと言わない |
| 固定設定の矛盾 | 構造化した矛盾はブロック。文章意味の疑いは指摘と作者確認 |
| 秘密情報・公開projection | 公開DTOの契約テストで漏えいを防ぐ |
| 文字数・地点数・ジョブ予算 | サーバー側の設定上限を適用 |

時間・警戒など数値状態は上限・抽象化を決め、無限状態探索にしない。自由行動を含む全経路の正常性は保証できない。警告は作者が内容を確認して記録する。

試遊開始時にplaytest版を作成し、通常Campaignと同じエンジンで実行する。編集しても進行中の試遊へ反映しない。「最新原稿でやり直す」は別runを作る。作者専用デバッグでフラグ・遷移理由・結末条件を表示し、通常プレイには出さない。

初期の公開条件は、ブロッキングエラーなし、警告の確認、同一コンパイル内容で作者自身が少なくとも1結末まで試遊済みとする。デバッグで状態を強制変更したrunやエージェントだけの完走を作者試遊の代用にしない。公開メタデータだけの変更は、compiled hashが同じなら試遊実績を再利用できる。全結末の人手確認や面白さの保証を公開条件と混同しない。

## 10. AI作成補助と非同期実行

ユーザー操作は「整合性チェック」「不足部分の補完」「条件から構成案を作成」「採用した構成を実行定義候補へ変換」。自動保存ではLLMを呼ばない。

自然文生成は構成案 → 作者による確認・修正 → 具体化 → 機械検証 → 提案 → 作者採用 → 試遊の順序。生成物も手動作品と同じ検証を通す。

ジョブ状態はqueued / running / succeeded / failed / cancelled。base_revisionと入力snapshot/hashを固定する。worker lease・timeout・試行回数・呼出回数・token上限を設け、再配信で結果を重複確定しない。provider側で冪等性が提供されない呼出の重複課金まで保証できないため、試行履歴と使用量を記録する。

AIがrevision 10を処理中に作者が11を保存したら、結果は10への提案として保存し、11へ自動適用しない。初期は再生成または手動差分採用とし、自動mergeは行わない。採用はexpected_revision、所有者、固定項目を再検証して新revisionを作成する。モデルが出した「固定解除」などの権限変更命令は採用しない。

既存のDB workerの運用知識を再利用し、authoring jobはゲームTurnのtransactionと分離する。大量の生成がプレイを止めないよう別の同時実行枠・queue処理を設ける。作者原稿はデータとして扱い、system指示・任意ツール実行権限として扱わない。

## 11. API案

既存/adventures APIを活用し、新旧契約は移行期に明示的に共存させる。以下は予定の境界であり既存endpointではない。

| 操作 | API案 | 重要な契約 |
| --- | --- | --- |
| 自作品一覧・作成 | GET/POST /stories/mine またはPOST /stories | 所有者は認証principalから取得 |
| 原稿取得・保存 | GET/PATCH /stories/{id}/draft | expected_revision、request_id、変更内容 |
| 履歴・復元 | GET /stories/{id}/revisions、POST /stories/{id}/restore | 復元先も新revision |
| テンプレート一覧 | GET /story-templates | supported capabilities、version |
| 検証 | POST /stories/{id}/validate | 保存済みrevision対象 |
| 試遊開始 | POST /stories/{id}/playtests | snapshot固定、request_id |
| 公開 | POST /stories/{id}/publish | revision、検証report、公開範囲、request_id |
| 公開停止 | POST /stories/{id}/withdraw | 作者権限、監査 |
| 複製 | POST /stories/{id}/duplicate | 自作品のみ、新しい作品ID |
| 公開一覧・詳細 | GET /stories、GET /stories/{id} | 公開projectionのみ、cursor pagination |
| 通常プレイ開始 | POST /adventures | story_version_id追加、開始権限検査 |
| AIジョブ | POST /stories/{id}/authoring-jobs、GET /authoring-jobs/{id} | 種別・base_revision・request_id |
| 提案採用 | POST /stories/{id}/proposals/{proposal_id}/apply | expected_revision、選択した変更 |
| 通報・運営停止 | POST /stories/{id}/reports、管理者用moderation API | 別権限、監査記録 |

認証不備401、編集権限不足403、存在を隠す必要のあるprivate作品404、revision/公開版競合409、入力・検証不正422、quota超過429を基準とする。値の推測やIDの指定だけでは権限を得られない。

## 12. 公開、認可、費用管理

| 利用者 | 可能なこと |
| --- | --- |
| 未ログイン閲覧者 | public作品の公開情報を閲覧。unlistedはURLを知る場合のみ |
| 認証済みプレイヤー | public/unlistedの現在版を開始、自分のrunを再開 |
| 作者 | 自作品の原稿・履歴・提案閲覧、編集・試遊・公開・停止・複製 |
| 管理者 | 通報確認、停止・解除。管理操作を監査 |

unlistedは検索・一覧に出さないURL共有で、秘密のアクセス制御ではない。privateは作者だけ。招待制アクセスが必要な場合は後からACLを追加し、unlistedの意味を変えない。

既存OIDC事前登録を限定公開段階で使う。一般公開段階で確認済みidentityからprincipalを作る登録フロー、無効化、最低限の表示プロフィールを追加する。作者は他人の会話ログを初期状態では閲覧できず、プレイ数・完了数など集計のみ取得する。将来ログ共有するならプレイヤーの明示同意を別機能として設ける。

公開DTO・作者DTO・player state DTOを分け、未発見の秘密や結末をブラウザへ一括送信しない。解決用LLMには必要な秘密を限定して渡し、描写用には公開可能な確定事実だけを渡す。ユーザー文章はHTMLとして実行しない。

一般公開前に生成・検証・プレイのユーザー別quota、同時実行上限、リクエストサイズ上限、運営の全体予算上限を設ける。利用量は用途別に記録し、収益化未実装を無制限利用の理由にしない。通報・停止・監査の最小運用を実装する。

## 13. 移行と受入の不変条件

既存2作品と保存済みv1/v2/v3を削除しない。旧ref/versionから版IDへの一意な対応を作り、再実行可能なimport/backfillを行う。既存runのscene/entity IDsや確定状態を再生成しない。旧版に必要な互換reader/policyを残す。全importが済むまで組み込みfallbackを撤去しない。

受入の核:

1. 本体コード変更なしで3本目の異なる作品を登録し、開始・結末・再開ができる。
2. 作者Aが作成した作品をBが遊べる。Aが第2版を公開してもBは第1版を継続する。
3. 未完成の原稿を保存でき、複数タブ・応答喪失・AI生成競合で上書きしない。
4. 新テンプレートを追加しても既存作品は変わらない。
5. 権限外編集、未発見情報漏えい、運営停止後の実行を防ぐ。
6. LLM再試行・ジョブ再配信・公開再送で状態が重複確定しない。

## 14. 未決定値と既定方針

合意済みの境界を再確認するために実装を止めない。地点数・文章量・token・quota・履歴保持日数の具体値、provider/model、フォーム部品の詳細、一般公開時の表示名ルールは実測と運用予算で決める。初期は短編テンプレート3〜5地点、保存debounce 2秒を提案値とし、上限やSLAとして確定した値とは区別する。

新しいゲームルール、共同編集、リミックス権限、作品間成長、秘密の招待ACLは将来機能であり、今回の基本実装に含めない。
