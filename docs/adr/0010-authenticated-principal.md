# ADR-0010: 認証済みprincipalをApplicationへ渡す固定境界

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

Bearer token、session cookie、外部IdPなど認証方式の選定とは独立に、transport adapterからApplication use caseへ次の不変なvalue objectを渡す。

```python
@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    principal_id: UUID
    issuer: str
    subject: str
    authenticated_at: datetime
    auth_context: frozenset[str]
    credential_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RequestContext:
    principal: AuthenticatedPrincipal
    request_id: UUID
    correlation_id: str
```

`principal_id` は内部の安定IDで、認証adapterが `(issuer, subject)` から解決する。`auth_context` は `mfa` 等の認証強度を表す不透明な値であり、Campaign roleやresource権限をtoken claimからコピーしない。`credential_expires_at`はtransport非依存のcredential有効期限で、値がある場合はtimezone-aware UTCとする。Applicationは `principal_id` を使って最新のCampaign membership、actor操作権、item所有権をrepository/policyで毎回認可する。

本番APIは`AIRPG_AUTH_ISSUER`で指定した一つのOIDC IssuerとBearer JWTだけを受け付ける。署名、Issuer、Audience、必須claim、期限を検証した後、`(issuer, subject)`が`principal_identities`へ事前登録されている場合だけ、対応する安定した`principal_id`を生成する。identity行は無効化できるが、このreleaseでは別principalへの付け替え、削除、再有効化を許可しない。

Campaign認可は引き続きDB-backedとし、JWTのrole、email、nameなどのclaimを無視する。SSEは`credential_expires_at`へ到達した時点で静かに閉じ、接続中もCampaign membershipをpollごとに再確認する。

未認証requestではこの型を作らずHTTP adapterで401にする。認証済みだが権限がない場合はApplicationのauthorization errorをHTTP adapterが403へ変換する。SSE接続、Turn POST、worker起動元で同じprincipal IDを維持し、worker jobには完全なcredentialやtokenを保存せず `principal_id` と受付時の監査情報だけを渡す。サービス内部のjobは別の `ServicePrincipal` variantとし、player principalを偽装しない。

## 採用理由

- 認証製品が未決定でもApplicationの入力と監査主体を固定できる。
- identity確認（authentication）とCampaign内の操作可否（authorization）を分離できる。
- token、cookie、FastAPI requestをuse caseやDomainへ漏らさず、期限切れcredentialをqueueに保存しない。

## 却下した案

- **raw JWT/HTTP headerをApplicationへ渡す**: transportとIdP固有claimへ結合し、各use caseで検証が分散する。
- **emailをprincipal IDにする**: 変更・再利用可能な表示属性を永続的な主体IDにできない。
- **role/権限をprincipalへ固定する**: Campaignごとに異なる権限とmembership失効を正しく扱えない。
- **認証方式決定まで境界を保留する**: handlerやworkerに暫定的なユーザー表現が拡散する。

## 後から交換できる境界

HTTP層の `Authenticator` portがcredentialを検証し、このvalue objectを生成する。将来のブラウザログイン／server session adapterも同じ`AuthenticatedPrincipal`契約を生成し、`(issuer, subject)` の一意対応、内部 `principal_id`、RequestContext契約を維持する。認可は独立した `AuthorizationPolicy` portに保つ。

## 影響

ログには `principal_id` とcorrelation IDを記録できるがtoken、Authorization header、cookie、認証秘密、subject、全claimは記録しない。subjectはidentity対応キーとして`principal_identities`だけに永続化し、管理者が明示的に実行したidentity管理CLIのJSON以外へ出力しない。event、ゲームデータ、HTTPエラーにもsubjectを含めない。日時はtimezone-aware UTCとする。issuer/subjectの紐付け変更には監査可能なidentity migrationが必要になる。
