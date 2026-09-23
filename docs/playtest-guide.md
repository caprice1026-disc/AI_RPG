# 短編のプレイテスト

## プレイヤーへ渡すもの

管理者はゲームのURLと、登録済みのログインアカウントを参加者へ渡します。プレイヤーはUUID、APIキー、DB接続情報を入力しません。初回は「ログインして冒険を始める」から進み、シナリオ、冒険者のタイプ、名前を選びます。

行動候補は、サーバーに登録された行動をそのまま実行します。自由入力も利用できます。判定や敵の反撃でHPが減るため、所持品と撤退の候補も確認してください。失敗・撤退・敗北も短編の結末として扱います。

進行はサーバーへ保存されます。ブラウザを閉じた後は、同じアカウントでログインして「続きから」を選びます。送信結果が不明と表示された場合は「同じ要求を再送」を使います。別の要求を作り直すと、元の要求がすでに受け付けられているか判別できなくなるためです。

## 確認してほしいこと

最初の試遊は追加説明なしで進め、迷った箇所をメモします。途中でブラウザを一度閉じて再開し、いずれかの結末まで到達してください。20〜30分は短編の目標時間であり、実際の所要時間を保証するものではありません。

- 何をすればよいか分からなかった場面
- 入力した意図と違う結果になった場面
- 待ち時間や操作のしにくさを感じた場面
- 結末まで到達できたか、続きを再開できたか、もう一度遊びたいか

画面の感想欄は任意です。記入内容はテキストファイルとして手元に保存し、主催者へ任意の方法で渡します。サーバーへの自動送信はありません。氏名、メール、パスワード、APIキーなどは書かないでください。会話全文や認証情報も自動添付しません。

主催者は[記録テンプレート](playtest-record-template.md)へ結果をまとめます。人による試遊、エージェントのブラウザ操作、Fake LLMの自動テストを分けて記録し、一方の成功を他方の成功として扱いません。

## 管理者の本番設定

既存のPostgreSQLへ最新migrationを適用します。OIDC providerにはAuthorization Code Flow対応のclientを登録し、redirect URIを`https://ゲームのorigin/auth/callback`へ完全一致で設定します。PKCE S256を有効にし、implicit flowとpassword grantは利用しません。

```dotenv
AIRPG_AUTH_ISSUER=https://identity.example/realm
AIRPG_AUTH_AUDIENCE=ai-rpg-api
AIRPG_AUTH_ALLOWED_ALGORITHMS=RS256
AIRPG_AUTH_CLIENT_ID=ai-rpg-browser
AIRPG_AUTH_APP_ORIGIN=https://game.example
# Confidential clientを使う場合だけ、環境のsecretとして設定する。
# AIRPG_AUTH_CLIENT_SECRET=...
```

`AIRPG_AUTH_AUDIENCE`は既存Bearer API向け、`AIRPG_AUTH_CLIENT_ID`はブラウザのID token向けです。値はIdP側の登録と合わせます。issuerはDiscoveryの値と完全一致させます。app originにpathは指定できません。国際化ドメインはブラウザが使うASCIIのpunycode表記で指定します。

IdPで確認したsubjectを、管理CLIで事前登録します。subjectの取得方法はIdPの管理機能に従い、プレイヤー本人へ入力を求めません。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\ai-rpg.exe auth register --subject "IdPで確認したsubject"
.\.venv\Scripts\ai-rpg.exe api
# 利用を停止するとき
.\.venv\Scripts\ai-rpg.exe auth disable --subject "IdPで確認したsubject"
```

GeminiのAPIキーはサーバーの`.env`またはsecret設定だけに置きます。ブラウザ用の`VITE_`変数へコピーしません。APIと解決・描写workerは同じDBへ接続し、workerには現在のREADMEの起動方法を使います。

HTTPSのreverse proxyを使い、callback query、Authorization、Cookieをaccess logへ残さないよう設定してください。標準CLIはaccess logを無効化しますが、外側のproxyのログ設定までは変更しません。ブラウザのセッション期限は8時間で、失効した場合は再ログインが必要です。アプリからのログアウトはIdP全体のSSO logoutではありません。

## 手元で実OIDCを確認する

`compose.oidc.yml`は[Keycloak公式イメージ](https://www.keycloak.org/getting-started/getting-started-docker)を使うローカル専用のサンプルです。既知のテスト用パスワードとHTTPを利用するため、公開環境では使いません。ポート8180はloopbackだけへ公開します。

```powershell
docker compose -p ai-rpg-local -f compose.oidc.yml up -d
$env:AIRPG_AUTH_ISSUER = "http://127.0.0.1:8180/realms/airpg-local"
$env:AIRPG_AUTH_AUDIENCE = "airpg-api"
$env:AIRPG_AUTH_CLIENT_ID = "airpg-browser"
$env:AIRPG_AUTH_APP_ORIGIN = "http://127.0.0.1:8035"
$env:AIRPG_AUTH_ALLOW_INSECURE_LOOPBACK = "true"
# AIRPG_DATABASE_URLには専用の開発DBを指定する。
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\ai-rpg.exe auth register --subject "00000000-0000-0000-0000-000000000501"
.\.venv\Scripts\ai-rpg.exe api --port 8035
```

`http://127.0.0.1:8035/`を開き、ユーザー名`adventurer`、パスワード`local-playtest-only`でログインします。IdPにだけ存在する`unregistered`アカウントも同じテスト用パスワードで利用でき、アプリ側の未登録拒否を確認できます。サンプルrealmを削除するとIdP側のテストユーザーも消えます。既存のゲームDBは削除しません。

検証終了時はAPIとworkerを停止し、起動時と同じcompose project名で`docker compose -p ai-rpg-local -f compose.oidc.yml stop`を実行します。他のPostgreSQLコンテナやWSL全体を止める必要はありません。
