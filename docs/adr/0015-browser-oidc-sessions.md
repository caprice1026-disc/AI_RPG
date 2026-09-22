# ADR-0015: ブラウザOIDCとサーバー管理セッション

## 状態

採用。ADR-0010のBearer認証と事前登録制を維持し、ブラウザ用adapterを追加する。

## 決定

ブラウザはAuthorization Code FlowとPKCE S256でOIDC認証する。Discoveryで得た固定issuerのendpointだけを利用し、stateとブラウザに結び付けたCookieを照合してログイン要求を一度だけ消費する。ID tokenは署名、issuer、client audience、azp、期限、iat、nonceを検証する。

成功時は既存のidentity対応表を参照する。未登録または無効なidentityに対してユーザーを自動作成しない。対応表へ不変のidentity_idを追加し、セッションからはこのIDだけを参照する。subjectは引き続き対応表だけに保存する。

ブラウザへ渡すのはランダムなセッションCookieだけとし、providerのID/access/refresh tokenを保存しない。DBにはCookie秘密値のSHA-256を保存する。CookieはHttpOnly、SameSite=Lax、Path=/、本番Secureとし、__Host-接頭辞を使う。セッションはログインから8時間で失効する。ID tokenの期限はログイン時に検証し、セッションの期限とは分ける。refresh tokenによる延長は行わない。

Cookieで認証する更新要求は、固定app originとCSRF tokenを両方照合する。CSRF tokenは認証済みの同一originから取得できるsession APIで返す。Bearerを明示した要求は従来の検証を優先し、失敗してもCookieへ切り替えない。各requestとSSEのpollでsession期限・identity無効化・logoutを確認する。

ログイン試行は5分で失効する。state、binding、nonceはhashで保存し、code verifierだけはcode交換に必要なため短期間DBへ保存する。消費後に行を削除し、期限切れ行は次の作成時に掃除する。sessionも作成時に期限切れ行を掃除する。

ログアウトはこのアプリのsessionを失効させる。IdP全体のSSO logoutは行わないため、再ログイン時にIdPがパスワードを要求しない場合がある。

## 運用上の制約

本番issuer、Discoveryの各endpoint、app originはHTTPSとする。ローカルIdPを使う検証に限り、明示設定でloopback HTTPを許可する。この設定または開発principalを使うCLIは公開interfaceへbindできない。本番の認証制約を解除する設定としては使わない。

callback URLのqueryには認可codeが含まれるため、標準CLIではaccess logを無効化する。別のASGI起動・reverse proxyを使う場合も、query、Cookie、Authorization headerを記録しない設定が必要になる。認証レスポンスと公開状態はno-storeとし、Referrer-Policy: no-referrerを付ける。

## 検証

JWT・Discoveryの不正入力、別ブラウザのcallback、state再利用、期限切れ、未登録identity、CSRF/Origin、Bearer優先、失効をテストする。DBでは一度だけのconsumeとidentity無効化との競合を実PostgreSQLで確認する。実IdPでのブラウザ検証は段階5の実施記録へ残す。

## 参照

- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html)
- [OpenID Connect Discovery](https://openid.net/specs/openid-connect-discovery-1_0.html)
- [OAuth 2.0 Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html)
