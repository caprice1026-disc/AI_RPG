# ADR-0005: Server-Sent Eventsによる一方向ストリーミング

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

Turn投入は通常のHTTP POST、サーバーからクライアントへのイベント配信は **Server-Sent Events (SSE)** とする。ストリームはCampaignと認証済みprincipalの公開範囲に固定する。永続イベントには単調増加するCampaign sequenceをSSE `id` として付与し、再接続時は `Last-Event-ID` をcursorとして認可済みprojectionを再送する。配信はat-least-onceで、クライアントはevent IDで重複排除する。

`narration.delta` のような一時chunkは再送対象外とし、再接続後は保存済みTurn表現を送る。heartbeat commentを15秒ごとに送信し、proxy bufferingを無効化する。遅いsubscriberのためにDB transactionやworkerを停止させず、接続を閉じて再接続・catch-upさせる。

## 採用理由

- MVPの通信は「コマンドはHTTP、更新はserverからclient」の一方向で足りる。
- HTTPの認証・proxy・観測基盤を再利用でき、WebSocket独自の再接続protocolを減らせる。
- `Last-Event-ID` と永続sequenceが既存のat-least-once配信設計に適合する。

## 却下した案

- **WebSocket**: 双方向低遅延通信が必要になるまで、接続状態、再認証、独自ackを持つ複雑さを正当化できない。
- **long polling**: 実装は単純だが、描写chunkと進行イベントの頻繁な更新に余計なrequestが増える。
- **LLM providerのstreamを直接中継**: provider障害や未確定出力をゲームの確定イベントと分離できない。

## 後から交換できる境界

Applicationが発行するtransport非依存の `PublicEvent`（id、type、schema_version、payload）をHTTP adapterがSSEへencodeする。WebSocketへ追加・交換する場合もevent schema、cursor、認可projection、重複排除規約を維持する。

## 影響

イベントpayloadはversion管理し、秘密を含むraw domain eventを直接送らない。SSE endpointにも通常APIと同じprincipalを要求し、接続後もCampaign membership失効を有限時間内に反映する。
