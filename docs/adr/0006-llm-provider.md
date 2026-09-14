# ADR-0006: port/adapterによるLLM抽象化とOpenAI初期対応

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

Application側には用途別の `NarrativeGenerator`、`IntentExtractor`、`ResultNarrator` Protocolを置き、共通のrequest、型付きresult、usage、provider request ID、終了理由を返す。初期adapterは **OpenAI API** に対応し、構造化Intentはproviderが提供するstructured output機能へadapter内で変換する。モデルIDはコードへ固定せず、`FAST`、`QUALITY`、`BACKGROUND` tierから設定で解決する。

timeout、呼び出し予約、retry禁止、schema検証、provider例外からApplication errorへの変換はadapter境界で統一する。API keyはsecret storeから注入し、prompt、秘密値、provider SDK型をDomainへ保存しない。保存が必要な監査情報はprovider名、model ID、request ID、token usage、latency、schema versionに限定する。

## 採用理由

- 最初のproviderを定めることで実装と評価を開始しつつ、ゲームルールをprovider SDKから独立させられる。
- 用途別portにより、「何でもできるLLM client」がApplicationの権限境界を迂回するのを防げる。
- tier設定によりモデル更新をDomain変更なしで行える。

## 却下した案

- **provider SDKをuse caseから直接呼ぶ**: SDK型、retry、エラー、model IDがApplication全体へ漏れる。
- **初期から複数providerへ同等対応**: MVPのschema差・stream差・評価範囲を不必要に増やす。
- **汎用的な単一 `generate()` port**: NarrativeとMechanicalで異なる出力権限を型で制約しにくい。

## 後から交換できる境界

provider adapterはProtocolに対するpluginとして登録し、contract testを共有する。prompt templateとprovider向けJSON schema変換もadapter/application service側でversion管理する。新providerはusage/error mappingと構造化出力の適合試験を満たせば追加でき、Game EngineやCanonical schemaを変更しない。

## 影響

OpenAI固有機能をDomain contractの必須要素にしない。fallbackやprovider切替は新たな物理呼び出しとしてADR-0008の予算を消費し、暗黙retryを行わない。
