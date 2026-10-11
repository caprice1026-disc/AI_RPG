# Gemini 3.6 Flash 移行・検証記録（2026-10-11）

調査基点: main `5483fa1386b022a627ac945fa77706aa350e86d9`。

## 変更と互換性

Googleの[3.5 Flash公式ページ](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash)は、3.5 Flashが非推奨となり、要求を3.6 Flashへ自動転送すると説明している。アプリの共通・FAST・QUALITY・BACKGROUNDの既定値とComposeのBACKGROUND fallbackを `google:gemini-3.6-flash` に変更した。既存の明示overrideや他providerはそのまま利用できる。過去の実API検証記録は、当時要求したモデルを示すため変更していない。

[3.6 Flash仕様](https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash)のモデルIDは `gemini-3.6-flash`、Structured OutputsとFunction Callingをサポートする。ロック済みPydantic AI 2.46.0 / google-genai 2.24.0を維持した。実SDKのGoogleModelとNativeOutputを通すMockTransportテストで、新IDへのHTTP要求、JSON Schema、アプリ側DTO検証を確認した。モデル名のためだけの依存更新は必要なかった。

作者向けcheck / fill / outline / concretizeの全用途を検証した。空原稿から返された完全な候補を提案→選択採用→compileまで通し、2 Scene・2 endingの実行可能な定義になることを確認した。応答はローカルfixtureであり、実モデルがその候補を生成したとの主張ではない。ゲームのintent / narrative / result narrationも既存HTTP契約テストで新モデルへ移行した。

## タイムアウト・予算・料金

ゲームLLMは30秒、作者向け生成は60秒、作者worker leaseは90秒を維持する。SDK retryは1 attempt、Agent retryは0、用途ごとの物理要求上限は1。既存workerの期限・予約処理を変えない。タイムアウト、429、壊れたJSON、打ち切り出力で隠れた再送がないことを検証した。実応答時間は未計測なので、モデル変更を理由に期限を延長していない。

[公式料金表](https://ai.google.dev/gemini-api/docs/pricing)を2026-10-11に確認した。Gemini Developer APIのStandard料金は以下（USD / 100万token、出力はthinkingを含む）。Vertex AIや他service tierの料金ではない。

| 適用期間 | 入力 | 出力 |
| --- | ---: | ---: |
| 2026-12-31まで | 0.75 | 3.75 |
| 2027-01-01から | 1.50 | 7.50 |

推定額は `(入力token × 入力単価 + thinkingを含む出力token × 出力単価) / 1,000,000`。例えば入力1,000・出力1,000 tokenなら2026年は$0.0045、改定後は$0.009。キャッシュ・検索・別料金tierは別計算する。

AI_RPGの予算管理はDBの呼出回数予約と日次上限、作者向けprompt byte / 最大出力8,192 / 合計24,000 token制限であり、月額USDでの停止機構はない。使用量とactual modelを記録し、thinking tokenも出力使用量へ含める。usage不明は無料と見なさない。合計tokenの超過検知は応答後にも行われるため、請求額の厳密な事前保証ではない。料金表の変更だけで架空のUSD上限を導入せず、この制約を明記する。

## 配備時の切替

1. 既存 `.env` / 配備環境の `AIRPG_LLM_MODEL` が3.5 Flashなら `google:gemini-3.6-flash` へ変更する。
2. 明示指定済みの `AIRPG_FAST_MODEL` / `AIRPG_QUALITY_MODEL` / `AIRPG_BACKGROUND_MODEL` も、3.5 Flashの完全一致だけを変更する。他モデルは維持する。
3. API・game worker・story workerを新設定で再起動する。Composeは既存環境変数を優先するため、コード既定値の変更だけでは既存overrideを置換しない。
4. 新規の検証用作品で構成案生成と具体化、検証・試遊を確認し、usage / actual_model / タイムアウトを確認する。ゲームの保存済み状態や公開版を書き換える必要はない。

配備先への接続情報はなく、稼働環境の変更は今回行っていない。

## 実行結果と制限

- `uv sync --frozen` で既存lockの依存を導入。
- `.venv/bin/python -m pytest tests/unit tests/contract -q -p no:cacheprovider`: **726 passed**（うち追加作者HTTP契約8ケース）。
- `.venv/bin/python -m pytest tests/integration -q -p no:cacheprovider`: **16 passed / 314 skipped**。PostgreSQLと実Gemini用の設定がなく、DB結合と実API smokeは未検証。
- `ruff check .`: 成功。`mypy src`: 88 source files成功。
- 初回のテスト収集には`python -m pytest`でrootをimport pathに含める必要があった。実行環境のSOCKS proxyに起因する既存認証テストの失敗は、検証環境へ`socksio==1.0.0`を追加して解消した。プロジェクトのlockや依存宣言は変更していない。

この環境にGemini APIキーはなく、実API要求は0件。生成内容の品質・実際のレイテンシ・対象Googleプロジェクトでの利用可否は未確認。PostgreSQL実行環境もなく314件を成功件数に含めない。Starlette由来の既存deprecation warningが1件残る。
