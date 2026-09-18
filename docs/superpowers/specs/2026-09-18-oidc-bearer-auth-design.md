# OIDC Bearer JWT 認証設計

- 日付: 2026-09-18
- 対象: AI RPG API の本番向け認証境界
- 状態: 承認済み設計

## 目的

単一のOpenID Connect Issuerが発行するBearer JWTをAPIで検証し、事前登録済みの外部identityを内部の安定した`principal_id`へ解決する。Campaign内の認可は既存のDB-backed policyに残し、JWTのroleや権限claimへ移さない。

現在はAPIクライアント向けBearer認証だけを実装する。将来、ブラウザのAuthorization Code + PKCE、server session、ユーザープロフィールを追加しても、ApplicationとDomainのゲーム処理を変更しない境界を維持する。

## 対象外

- ブラウザのログイン画面、redirect、callback、token persistence
- IdPへの動的client登録
- 複数Issuerの同時利用
- 管理APIまたは管理画面
- ユーザープロフィール、メールアドレス、表示名の保存
- token introspection、refresh token、logout連携
- JWT claimによるCampaign roleまたはresource権限の付与
- identityの別principalへの付け替え、無効化解除
- 新しいmetrics基盤の導入

## 採用方式

JWT検証にはPyJWT 2系と暗号依存を使用する。既存のhttpxでOpenID Provider Configurationを取得し、PyJWTのJWKS clientで公開鍵の選択、キャッシュ、鍵ローテーション時の再取得を行う。同期的なJWKS取得はFastAPIのevent loopを止めないようworker threadへ逃がす。

AuthlibはOAuth/OIDC client全体を必要とする段階で再評価する。現時点ではログインフローを実装しないため導入しない。JOSE/JWTを独自実装しない。

## アーキテクチャ

HTTP層に`OidcBearerAuthenticator`を置く。AuthenticatorはFastAPIのdependencyとしてAuthorization headerを受け取り、検証済みの`AuthenticatedPrincipal`だけをApplication use caseへ渡す。

処理の流れは次のとおり。

1. API起動時に設定済みIssuerの`/.well-known/openid-configuration`をhttpxで取得する。
2. Discovery文書の`issuer`が設定値と完全一致し、`jwks_uri`がHTTPS URLであることを確認する。
3. requestの`Authorization: Bearer <JWT>`を抽出する。
4. 設定で許可した非対称署名アルゴリズムだけを使い、公開鍵、署名、`iss`、`aud`、`sub`、`exp`を検証する。`nbf`と`iat`は存在する場合に検証する。
5. `(issuer, subject)`をPostgreSQLの事前登録identityから`principal_id`へ解決する。
6. `AuthenticatedPrincipal`を既存のTurn、query、SSE use caseへ渡す。
7. Applicationは従来どおり`principal_id`を使い、最新のCampaign membershipとactor権限をDBで確認する。

token、FastAPI Request、OIDC固有claimはApplicationまたはDomainへ渡さない。`auth_context`は初期実装では空集合とし、role、email、nameなどをコピーしない。

## Principal契約

`AuthenticatedPrincipal`へ次を追加する。

```python
credential_expires_at: datetime | None = None
```

OIDC adapterはJWTの`exp`をtimezone-aware UTCへ変換して設定する。開発用principalや将来の期限を持たない内部認証は`None`を使用できる。値がある場合はUTCでなければならない。Authenticatorは、clock skewを考慮したJWT検証を通過しても`exp`が認証時刻以前ならprincipalを生成しない。

この追加はtransport非依存のcredential有効期限であり、raw tokenやIdP固有型を境界へ持ち込まない。将来のserver session adapterも同じ項目を利用できる。

## 設定と起動

本番Bearer認証に必要な設定は次の3つとする。

```text
AIRPG_AUTH_ISSUER=https://idp.example.com/
AIRPG_AUTH_AUDIENCE=ai-rpg-api
AIRPG_AUTH_ALLOWED_ALGORITHMS=RS256
```

`AIRPG_AUTH_ALLOWED_ALGORITHMS`はカンマ区切りの明示的なallowlistとし、初期値は`RS256`とする。対称鍵方式は許可しない。JWT headerやDiscovery文書だけを根拠にallowlistを拡張しない。小さなclock skewだけを許容し、既定値は30秒とする。

`ai-rpg api --dev-principal <UUID>`はローカル開発専用の明示的な迂回路として維持する。`--dev-principal`なしで起動する場合は上記設定を必須とし、設定不備、Discovery取得失敗、不正なDiscovery文書ではprocessを起動失敗させる。`create_app()`のdependency injectionはunit/integration testのため維持する。

Discovery文書はprocess起動時に一度だけ読み込む。JWKSはPyJWTのTTL cacheを使い、未知の`kid`ではcooldownを守って再取得する。Discovery/JWKSのtimeoutは5秒、JWKS cache TTLは300秒、未知`kid`による再取得cooldownは30秒とする。

## 永続モデル

### `principals`

内部主体の安定した基点を表す。

- `id uuid primary key`
- `created_at timestamptz not null default now()`

将来のユーザープロフィールや複数identityはこのIDへ関連付ける。現段階ではプロフィール属性を持たせない。

### `principal_identities`

外部identityと内部主体の対応を表す。

- `issuer text not null`
- `subject text not null`
- `principal_id uuid not null references principals(id)`
- `created_at timestamptz not null default now()`
- `disabled_at timestamptz null`
- composite primary key: `(issuer, subject)`
- check: `disabled_at is null or disabled_at >= created_at`

有効な行だけを認証解決に使用する。DB triggerで行の削除、issuer/subject/principal_id/created_atの変更、`disabled_at`の巻き戻しまたは再変更を拒否する。無効化は`disabled_at`を未設定から一度だけ設定する操作とする。これにより登録先と登録・無効化時刻をDBに残す。

既存の`campaign_members`へ新しい外部キーは追加しない。既存fixtureと認可経路への変更を最小にし、production authenticationに必要なidentityだけを`principals`へ登録する。

ORM mappingはInfrastructure層へ置き、通常の登録、解決、無効化にはSQLAlchemy ORMまたは式を使う。CLI操作は一つのDB transactionで完了させる。

## 管理CLI

管理操作はローカルCLIだけを提供する。

```text
ai-rpg auth register --subject <OIDC_SUBJECT> [--principal-id <UUID>]
ai-rpg auth disable --subject <OIDC_SUBJECT>
```

Issuerは`AIRPG_AUTH_ISSUER`から取得し、command引数では変更できない。これにより単一Issuer構成で別Issuerを誤登録しない。

`register`の挙動:

- `principal-id`省略時は新しいUUIDを発行する。
- 対応する`principals`行がなければ作成する。
- 同一の有効な対応を再登録した場合は成功として既存値を返す。
- 同じidentityが別principalへ登録済み、または無効化済みの場合は変更せず失敗する。
- 結果を`issuer`、`subject`、`principal_id`、状態を含むJSONで標準出力へ返す。

`disable`の挙動:

- 有効なidentityへ現在のUTC時刻を設定する。
- すでに無効なら既存状態を返して成功する。
- 未登録なら変更せず失敗する。
- tokenや秘密値を入力・表示・保存しない。

付け替えや再有効化が必要になった場合は、監査主体を確立した将来の管理機能または明示的なmigrationとして追加する。

## HTTPエラー

外部へ返す認証エラーは情報漏えいを避けて統一する。

- Authorization headerなし、Bearer形式不正、署名不正、許可外algorithm、claim不正、期限切れ、未登録identity、無効identity: `401`、`{"detail":{"code":"UNAUTHENTICATED"}}`、`WWW-Authenticate: Bearer`
- 起動後のJWKS取得・更新が一時的に失敗し、cached keyでも検証できない: `503`、`{"detail":{"code":"AUTHENTICATION_UNAVAILABLE"}}`
- 認証済みだがCampaign membershipまたはactor権限がない: 既存どおり`403`

JWT parser由来の詳細、`kid`、subject、token断片をHTTP応答へ含めない。

## SSE

SSEは接続開始時に通常のBearer認証を行う。event loop内で`credential_expires_at`を確認し、期限へ到達したら静かにstreamを終了する。Campaign membershipは既存どおりpollごとに再確認し、失効時にstreamを終了する。

identity無効化は以後のPOST、GET、新規SSEへ直ちに反映する。接続済みSSEはread-onlyであり、初期実装ではJWT期限またはCampaign membership失効まで維持する。identity無効化のpush通知やtoken revocationはserver sessionまたはuser管理を導入する段階で扱う。

## ログと運用

新しい監視製品やmetrics endpointは追加しない。既存loggingで次の分類を秘密情報なしに記録できるようにする。

- 認証成功
- credential拒否
- identity未登録または無効
- Discovery/JWKS障害
- API起動時の認証設定不備

token全文、Authorization header、全claim、メールアドレス、subjectは記録しない。成功後は内部`principal_id`とIssuerだけを利用できる。拒否理由は運用ログでは分類できるが、外部HTTP応答は共通の401とする。

## テスト戦略

外部IdPへ依存せず、テスト用RSA keyとmock HTTP transportで検証する。

### Unit

- 設定値、単一Issuer、algorithm allowlist、UTC expiryのvalidation
- 正常なRS256 JWT
- 不正署名、許可外algorithm、`iss`/`aud`不一致
- `exp`切れ、未来の`nbf`、不正な`iat`
- 必須`iss`、`aud`、`sub`、`exp`の欠落
- Discovery issuer不一致、非HTTPS JWKS URL、不正JSON、timeout
- JWKS cache hit、未知`kid`、鍵ローテーション、取得失敗
- `AuthenticatedPrincipal.credential_expires_at`のvalidation

### PostgreSQL integration

- identity登録と解決
- 同一登録と同一無効化の冪等性
- 別principalとの競合
- 未登録・無効identityの拒否
- identity mappingの変更・削除・再有効化拒否
- CLI transaction rollback

### API integration

- Bearerなし、不正Bearer、未登録identityが同じ401とheaderを返す
- 有効tokenが登録principalへ解決される
- 認証後のCampaign membership不足が403になる
- JWKS一時障害が503になる
- JWT期限とmembership失効でSSEが終了する
- 開発principal injectionが既存どおり動く

### 回帰確認

- 全unit、contract、integration、PostgreSQL test
- Ruff、mypy、build、lock確認
- 既存browser test
- mock Discovery/JWKS、管理CLI、APIを接続したローカル一往復

## 文書更新

READMEへ次を追加する。

- production APIのOIDC環境変数
- identity登録・無効化CLI
- `--dev-principal`が開発専用であること
- 401、403、503の意味
- tokenやrole claimを保存・信用しない方針

ADR-0010へ事前登録方式、credential expiry、接続済みSSEの期限処理を追記する。将来のブラウザログインは新しいAuthenticator adapterとして追加し、Application境界を維持する。

## 完了条件

- migration、ORM mapping、identity repository、OIDC authenticator、管理CLIが実装されている。
- tokenとIdP固有claimがApplication/Domainへ漏れていない。
- Campaign認可が引き続きDB-backedである。
- 無効または未知identityをfail closedで拒否する。
- JWT期限とmembership失効がSSEに反映される。
- READMEとADRが実装内容に一致する。
- 変更範囲に応じた全検証が成功する。
