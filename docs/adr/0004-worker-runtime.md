# ADR-0004: PostgreSQL lease queueを用いた独立async worker

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

Turn解決workerと確定後の描写workerは、APIとは**別プロセス・別デプロイロール**のPython asyncio workerとして実行する。MVPでは専用brokerを増やさず、PostgreSQLのjob/turn行をdurable queueとし、短いtransaction内の `FOR UPDATE SKIP LOCKED` とlease/owner epochで取得する。通知遅延の短縮にはPostgreSQL `LISTEN/NOTIFY` をヒントとして使ってよいが、通知を正本にせず定期pollで必ず回収する。

Turn解決と描写は別のjob種別、状態、lease ownershipを持つ。workerは冪等なApplication use caseを呼び、停止後はlease失効により再取得される。水平scale時も同じ行を同時所有せず、古いownerのcommitはepochで拒否する。

## 採用理由

- 既存設計のTurn、transaction、lease、idempotencyをそのままdurabilityの根拠にできる。
- Redis等をMVPの必須構成に追加せず、API切断やprocess再起動から処理を復旧できる。
- Turn確定と描写再生成を別々にscale・停止でき、描写timeoutでゲーム結果を再適用しない。

## 却下した案

- **FastAPI BackgroundTasks/in-process asyncio task**: process終了時のdurabilityと複数instance間の所有権を保証できない。
- **Celery/Dramatiq + Redis/RabbitMQ**: 将来候補だが、MVPではDBとの二重書込みやbroker運用が増える。
- **serverless functionのみ**: 実行時間、再試行、同時実行の挙動をproviderへ強く結合する。

## 後から交換できる境界

Applicationは `JobDispatcher.enqueue(job_type, aggregate_id)` とworker use caseだけを定義し、poll方式を知らない。job payloadにはIDとschema versionのみを置き、Canonicalな状態をbroker payloadに複製しない。将来brokerへ移行してもidempotency key、lease相当の所有token、DB上の処理状態を維持する。

## 影響

APIはTurn受付をcommitして202相当の応答を返せる。workerにはgraceful shutdown、lease更新、滞留件数、取得遅延、期限切れ再取得、dead-letter相当の失敗状態を計測する責務がある。
