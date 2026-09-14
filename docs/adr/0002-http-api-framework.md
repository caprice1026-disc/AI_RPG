# ADR-0002: FastAPIをHTTP APIフレームワークに採用

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

外部HTTP APIには **FastAPI** を採用し、ASGI server上で動かす。HTTPのrequest/response schemaとOpenAPI生成にはPydantic v2を使う。route handlerは入力変換、認証adapterの呼び出し、Application use caseの呼び出し、HTTPエラーへの変換だけを担い、ゲームルールやDB transactionを実装しない。

## 採用理由

- 既存契約がPydantic v2を前提としており、入力検証とOpenAPI表現を揃えやすい。
- ASGIによって通常APIとSSEの非同期I/Oを同じHTTP境界で扱える。
- dependency injectionを認証・DB sessionなどリクエスト単位のadapter構築に利用できる。

## 却下した案

- **Django + Django REST Framework**: 管理画面やORMを含む統合機能はMVPのport/adapter設計に対して過剰である。
- **Flask**: 採用可能だが、async streaming、schema、OpenAPIの統合に追加選定が必要になる。
- **Litestar**: 要件は満たすが、既存のPydanticベース設計からの移行利点が小さい。

## 後から交換できる境界

Application use caseはFastAPIの `Request`、`Depends`、`HTTPException` を受け取らない。HTTP adapterがtransport DTOをDomain/Application DTOへ変換する。別frameworkへ交換してもuse case、repository port、principal契約は変更しない。

## 影響

長時間処理はhandler内やFastAPIのin-process background taskで実行せず、ADR-0004のworkerへ渡す。HTTP接続終了とTurn処理の寿命を分離する。
