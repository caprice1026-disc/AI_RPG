# ADR-0003: SQLAlchemy 2、psycopg 3、AlembicによるPostgreSQLアクセス

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

PostgreSQLへのアクセスは **SQLAlchemy 2.x** のasync APIと **psycopg 3** async driverを用いる。schema migrationは **Alembic** のversioned revisionで管理する。本番DDL変更はアプリ起動時に自動実行せず、デプロイ工程の明示的な単一ジョブから `alembic upgrade head` を実行する。

ORM mappingはInfrastructure層に限定する。複合制約、部分index、`FOR UPDATE`、`SKIP LOCKED` など整合性・worker取得に必要なPostgreSQL機能は、SQLAlchemy式または局所的なparameterized SQLで明示する。migrationは原則として後方互換なexpand/migrate/contract順で行う。

## 採用理由

- unit of workと明示的transactionを使い、Canonical更新とevent追記のatomic性を表現できる。
- 一般的なCRUDとPostgreSQL固有機能の両方を扱え、手書きSQLだけに比べmappingの重複を減らせる。
- Alembic revisionをレビュー・ロールアウト単位にでき、schema変更を起動順に依存させずに済む。

## 却下した案

- **Django ORM/migrations**: HTTP frameworkまでDjangoへ結合しやすく、非同期workerとの共有境界が重い。
- **asyncpg + 手書きmigration**: 高性能だが、SQLとmapping、migration順序を維持する独自基盤が増える。
- **SQLModel**: API DTO、Domain model、永続modelを一つに寄せることは、本設計の境界分離と合わない。

## 後から交換できる境界

Applicationは `TurnRepository`、`UnitOfWork`、`EventRepository` 等のProtocolだけを参照する。SQLAlchemy model/sessionを返さずDomain DTOへ変換する。AlembicはDB schemaの履歴に限定し、Domainからimportしない。driverやORM交換時もrepository contractとmigration済みschemaを境界にする。

## 影響

async sessionを並列task間で共有しない。migrationにはupgrade、可能な範囲のdowngrade、既存データ移行方針を記載し、CIで空DBからheadまで適用検証する。
