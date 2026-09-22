# 段階5・Vue移行・実プレイ改善 ExecPlan

## 目的と完了条件

事前登録ユーザーがブラウザでログインし、UUIDやDB操作なしで冒険を開始・完走・再開できる状態にする。その後Vue 3 + Vite + TypeScriptへ移行し、desktop/mobileの実ブラウザで試遊して改善する。実Geminiの確認を含め、検証とmainへのpushまで行う。人による少人数試遊とエージェントの試遊は区別して記録する。

基点はmain f561cf8、作業場所は.worktrees/stage5-vue-playtest、branchはcodex/stage5-vue-playtest。PLANS.mdは存在しないため既存.agent計画形式を使う。

## Global Constraints

- Pydantic AIの3用途Agent、Engineの決定権、DB予算予約、lease、冪等性、公開情報境界を維持する。
- OIDCは単一issuer・Discovery・事前登録のみ。subjectはprincipal_identitiesだけに保存する。ログ・セッション・ゲーム記録に複製しない。
- Bearer APIを維持し、ブラウザはAuthorization Code + PKCE S256 + state + nonceで認証する。トークンをブラウザstorageへ渡さない。
- 不透明セッションCookieはHttpOnly/SameSite=Lax、本番Secure。DBにはセッション秘密値のSHA256だけを保存し、8時間の絶対期限とidentity無効化・logoutによる失効を確認する。
- Cookieでの更新要求はOriginとCSRF tokenを両方検証する。Bearerがあればそれを優先し、不正BearerからCookieへfallbackしない。SSEもsession/identity失効を検知する。
- 本番OIDCとapp originはHTTPS。ローカルIdP検証用HTTPは明示的設定かつloopback URLに限る。既定を弱めない。
- APIキー・code・token・Cookie・subjectをログへ出さない。外部通信はtimeoutとredirect制限を持つ。
- 正本のHP/在庫/現在地/履歴を表示し、未確認要求の同一request_id再送・409後の新規要求禁止・古い応答の拒否を維持する。
- 画像以外のUIはVue/HTML/CSSで実装する。新たな汎用frameworkや状態管理ライブラリは必要性が出るまで追加しない。

## Task 1: ブラウザ認証の永続化

担当: subagent。変更範囲はmodels.py、migrations/versions/0013_browser_sessions.py、infrastructure/postgres/browser_sessions.py、tests/integration/postgres/test_browser_sessions.pyに限定する。

principal_identitiesにidentity_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()を追加し既存行も埋める。既存issuer/subject PKとCLI登録契約は変えない。

login_attemptsはstate_digest(char相当Text PK)、binding_digest、nonce_digest、code_verifier、expires_atを持つ。browser_sessionsはtoken_digest PK、identity_id FK、csrf_token、created_at、expires_atを持つ。expires_at indexと期限整合checkを持つ。subjectやprovider tokenは保存しない。

PostgresBrowserSessionStore(session_factory)のasync API:

- create_login(*, state: str, binding: str, nonce: str, code_verifier: str, expires_at: datetime) -> None。state/binding/nonceはSHA256 hexで保存。期限切れattemptを掃除する。
- consume_login(*, state: str, binding: str, now: datetime) -> LoginAttempt | None。bindingと有効期限が一致した場合のみDELETE RETURNINGで一度だけ取得。LoginAttemptはnonce_digest:str, code_verifier:strを持つfrozen dataclass。
- create_session(*, issuer: str, subject: str, token: str, csrf_token: str, created_at: datetime, expires_at: datetime) -> UUID | None。有効な登録identityを行ロックで照合しsessionを作成、principal_idを返す。未登録/無効ならNone。tokenはSHA256 hexで保存。期限切れsessionを掃除する。
- resolve_session(*, token: str, now: datetime) -> BrowserSession | None。有効identityをjoinし期限を検証。BrowserSessionはprincipal_id:UUID, issuer:str, subject:str, authenticated_at:datetime, expires_at:datetime, csrf_token:strを持つfrozen dataclass。subjectはjoin結果としてメモリにのみ存在する。
- delete_session(token: str) -> None。hashで削除、未存在でも成功。

TDDで実DBの一度だけconsume/並行競合、誤binding、期限切れ、無効identity、logout、秘密値非保存、旧identity登録の互換性を検証する。自身の実装範囲のみcommitし報告する。他の既存testsの期待値変更が必要ならcontrollerへ知らせる。

## Task 2: OIDCブラウザフローとAPI統合

担当: main。api/browser_auth.py、api/auth.py、config.py、api/app.py、cli.pyと関連unit/API tests。

設定はauth_client_id、auth_client_secret(任意)、auth_app_origin、auth_allow_insecure_loopback(既定false)。ブラウザ設定は両方指定時だけ有効。固定callbackはorigin + /auth/callback。

GET /auth/loginは5分のlogin attemptとbinding Cookieを作りauthorization endpointへredirectする。GET /auth/callbackはstate/bindingを一度だけ消費しtoken endpointへPKCE付き交換、署名・iss/aud/azp・exp/iat/sub・nonceを検証。登録確認後新しいsession Cookieを発行しrootへ303する。GET /auth/sessionはprincipal_id/CSRF/expiry/modeだけを返す。POST /auth/logoutはCSRF検証し失効する。エラーは固定code、機密を含むcallbackのaccess logを出さない。

callback失敗、provider不通、未登録、logout、CSRF/Origin、Bearer互換、期限切れ、SSE失効、redirect注入拒否をテストする。開発principalモードは従来のloopback限定用途として維持し、session APIを使って画面が区別する。

## Task 3: 少人数試遊の受け渡しとフィードバック

説明なしで開始できる短いonboarding、開始/継続/結末と再挑戦、困った場面・意図違い・待ち時間の任意フィードバックを用意する。感想は画面上の自由記入欄からテキストファイルに保存する方式とし、収集専用DB/APIを増やさない。秘密値・subject・会話全文を自動添付しない。段階5の試遊手順、登録・失効・必要設定・未検証範囲をdocsへ記す。既存IdP指定がなければローカルIdPで本物のcode flowを確認する。

## Task 4: Vueのリッチな画面へ移行

ImageGenでゲーム画面conceptと礼拝堂artを作成し、Vue 3/Vite/TypeScriptで実装する。夜の藍色、温かい紙色、真鍮goldを共通tokenにし、物語を中央・状態を補助panel・入力を固定下部に置く。mobileは1columnで状態panelを折りたたむ。読みやすさ・focus・keyboard・reduced motionを維持する。

開始/再開、HP/在庫/目的/発見、履歴、選択肢、自由入力、戦闘反撃、結末、ログイン/失効/再送/feedbackを移植する。通信応答喪失・reload・古い応答・401・409・二重送信をVueテストで実行する。既存play-stateの回復規約を再利用し、互換テストを維持/移植してから旧HTMLを廃止する。

frontend/にsourceとpackage-lockを置き、build済assetはFastAPI static配下へ同梱する。Python単独のインストールでも画面を表示できる。Node依存はfrontend開発時だけ必要。型検証・component tests・buildを確認する。

実装分担: frontend/とVueテストをsubagent、FastAPIのstatic配信・旧画面削除判断・docs・実ブラウザをmainが担当する。build先はsrc/ai_rpg/api/static/vue、baseは/static/vue/。UIに表示する冒険・数値・候補はAPIの実データのみ。conceptの架空の冒険名は採用しない。

## Task 5: 実ブラウザ試遊、改善、統合

QA専用DBを使い、desktop/mobileでlogin→新規開始→探索→自由入力→戦闘/回復→結末→reload/再開→logoutを試す。通信中再読み込み、期限切れ/未登録、敗北/撤退も確認する。実Gemini利用は呼出数を記録し上限を設ける。課題は再現可能な回帰テストに落とし必要範囲の修正を行う。

全pytest(real PostgreSQL)、Ruff、mypy、frontend tests/typecheck/build、Python build、差分check、独立reviewを実施する。README・実施記録を更新しmainへ統合/pushしてremote SHA一致を確認する。検証用processだけを停止し、既存Docker/DB/別worktreeは変更しない。

## 進捗

- [x] 現行main/文書/認証契約を確認し専用worktreeとvenvを用意した。
- [x] Task 1: persistence (de5d5ec、13実DB tests、独立review承認)
- [x] Task 2: browser OIDC (Keycloak実ログイン、独立レビュー、Origin/SSE失効の回帰修正)
- [ ] Task 3: playtest handoff
- [ ] Task 4: Vue
- [ ] Task 5: browser/LLM/refinement/main
