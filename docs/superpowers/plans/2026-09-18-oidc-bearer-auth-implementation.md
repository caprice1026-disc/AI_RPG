# OIDC Bearer JWT Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 単一OIDC IssuerのBearer JWTを検証し、事前登録identityを内部principalへ解決する本番API認証と管理CLIを追加する。

**Architecture:** FastAPI dependencyがPyJWTでcredentialを検証し、PostgreSQLのidentity mappingをSQLAlchemyで解決して既存の`AuthenticatedPrincipal`へ変換する。Campaign認可は既存のDB-backed policyに残し、Discovery/JWKS、identity管理、HTTP error mappingをAPI/Infrastructure境界内に閉じ込める。

**Tech Stack:** Python 3.11、FastAPI、PyJWT 2.x、httpx、SQLAlchemy 2 async ORM、PostgreSQL 16、Alembic、pytest、argparse、uv

## Global Constraints

- 対応するIssuerは`AIRPG_AUTH_ISSUER`で指定する1つだけとする。
- `AIRPG_AUTH_AUDIENCE`を必須検証し、既定のalgorithm allowlistは`RS256`とする。
- Discovery/JWKS timeoutは5秒、JWKS cache TTLは300秒、未知`kid`のrefresh cooldownは30秒、clock skewは30秒とする。
- 対称鍵algorithm、`none`、token headerまたはDiscoveryだけを根拠にしたalgorithm追加を禁止する。
- 必須claimは`iss`、`aud`、`sub`、`exp`とし、`nbf`と`iat`は存在する場合に検証する。
- JWTのrole、email、nameをCampaign認可または`auth_context`へコピーしない。
- token、Authorization header、subject、全claimをDBまたはログへ保存しない。
- 未登録identityと無効identityは外部から区別できない同一401にする。
- `--dev-principal`は明示された開発APIだけで有効にし、workerや通常API起動へ暗黙適用しない。
- identityの付け替え、削除、再有効化は実装しない。
- 新しい管理API、管理画面、metrics製品、ブラウザログインは追加しない。
- 実装の正本は`docs/superpowers/specs/2026-09-18-oidc-bearer-auth-design.md`とする。

---

## File Map

### Create

- `migrations/versions/0007_oidc_identities.py`: principal/identity tablesと履歴保護trigger。
- `src/ai_rpg/infrastructure/postgres/identities.py`: identity登録、解決、無効化のasync ORM adapter。
- `src/ai_rpg/api/auth.py`: Discovery、JWT検証、FastAPI Bearer dependency、production builder。
- `tests/unit/test_oidc_auth.py`: Discovery、JWT、HTTP認証境界のunit test。
- `tests/unit/test_cli.py`: auth subcommandとAPI認証builder配線のunit test。

### Modify

- `pyproject.toml`: `pyjwt[crypto]>=2.14,<3`を追加する。
- `uv.lock`: uvでlockを更新する。
- `src/ai_rpg/config.py`: OIDC設定、comma-separated algorithm parser、production設定検証を追加する。
- `src/ai_rpg/application/auth.py`: optional credential expiryをprincipal契約へ追加する。
- `src/ai_rpg/infrastructure/postgres/models.py`: `PrincipalModel`と`PrincipalIdentityModel`を追加する。
- `src/ai_rpg/infrastructure/postgres/__init__.py`: identity adapterとerrorをexportする。
- `src/ai_rpg/api/__init__.py`: OIDC builderをexportする。
- `src/ai_rpg/api/app.py`:可変signatureのprincipal dependency、401 header、SSE expiry checkを追加する。
- `src/ai_rpg/cli.py`: `auth register`、`auth disable`、通常APIのOIDC startupを追加する。
- `tests/unit/test_auth.py`: credential expiry契約を検証する。
- `tests/unit/test_config.py`: OIDC設定の正常系とfail-fastを検証する。
- `tests/unit/test_postgres_models.py`: 新しいORM table/columnを検証する。
- `tests/integration/test_api.py`: 401 headerとSSE expiryを検証する。
- `tests/integration/postgres/test_migrations.py`: migration、identity lifecycle、CLI、mock OIDCの実DB契約を検証する。
- `README.md`: production OIDC設定と管理CLIを記載し、roadmapを更新する。
- `docs/adr/0010-authenticated-principal.md`: expiry、事前登録、SSE方針を記録する。

---

### Task 1: Principal expiry and validated OIDC settings

**Files:**
- Modify: `src/ai_rpg/application/auth.py`
- Modify: `src/ai_rpg/config.py`
- Modify: `tests/unit/test_auth.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `AuthenticatedPrincipal.credential_expires_at: datetime | None`
- Produces: `Settings.require_oidc() -> tuple[str, str, tuple[str, ...]]`
- Produces: `Settings.auth_issuer`, `auth_audience`, `auth_allowed_algorithms`
- Consumes: existing `AIRPG_` Pydantic settings boundary

- [ ] **Step 1: Add failing principal-expiry tests**

Append tests that require an optional UTC expiry and reject non-UTC values:

```python
def test_authenticated_principal_accepts_optional_utc_expiry() -> None:
    authenticated_at = datetime.now(UTC)
    expires_at = authenticated_at + timedelta(minutes=5)

    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="player",
        authenticated_at=authenticated_at,
        auth_context=frozenset(),
        credential_expires_at=expires_at,
    )

    assert principal.credential_expires_at == expires_at


def test_credential_expiry_must_be_utc() -> None:
    with pytest.raises(ValueError, match="credential_expires_at.*UTC"):
        AuthenticatedPrincipal(
            principal_id=uuid4(),
            issuer="test",
            subject="player",
            authenticated_at=datetime.now(UTC),
            auth_context=frozenset(),
            credential_expires_at=datetime.now(timezone(timedelta(hours=9))),
        )
```

- [ ] **Step 2: Run the principal tests and observe the missing field failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_auth.py -q -p no:cacheprovider
```

Expected: the new constructor argument is rejected before implementation.

- [ ] **Step 3: Extend the transport-independent principal contract**

Add a defaulted field after `auth_context` and validate timezone-aware UTC without imposing token-specific parsing:

```python
credential_expires_at: datetime | None = None

def __post_init__(self) -> None:
    if self.authenticated_at.tzinfo is None:
        raise ValueError("authenticated_atはtimezone-awareである必要があります")
    if self.authenticated_at.utcoffset() != timedelta(0):
        raise ValueError("authenticated_atはUTCである必要があります")
    if self.credential_expires_at is not None:
        if self.credential_expires_at.tzinfo is None:
            raise ValueError("credential_expires_atはtimezone-aware UTCである必要があります")
        if self.credential_expires_at.utcoffset() != timedelta(0):
            raise ValueError("credential_expires_atはUTCである必要があります")
    if not self.issuer or not self.subject:
        raise ValueError("issuerとsubjectは空にできません")
```

- [ ] **Step 4: Add failing settings tests**

Add exact coverage for absent production config, comma-separated algorithms, and symmetric algorithm rejection:

```python
def test_oidc_settings_parse_explicit_asymmetric_algorithms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRPG_AUTH_ISSUER", "https://idp.example.com/")
    monkeypatch.setenv("AIRPG_AUTH_AUDIENCE", "ai-rpg-api")
    monkeypatch.setenv("AIRPG_AUTH_ALLOWED_ALGORITHMS", "RS256,ES256")

    settings = Settings()

    assert settings.require_oidc() == (
        "https://idp.example.com/",
        "ai-rpg-api",
        ("RS256", "ES256"),
    )


def test_oidc_is_optional_for_non_api_processes() -> None:
    settings = Settings(auth_issuer=None, auth_audience=None)

    with pytest.raises(ValueError, match="AIRPG_AUTH_ISSUER"):
        settings.require_oidc()


@pytest.mark.parametrize("algorithm", ["HS256", "none", ""])
def test_oidc_rejects_unsafe_algorithm_allowlist(algorithm: str) -> None:
    with pytest.raises(ValidationError):
        Settings(auth_allowed_algorithms=algorithm)
```

- [ ] **Step 5: Implement minimal settings parsing and validation**

Keep the environment-facing algorithm value as a string so Pydantic Settings does not attempt JSON decoding before validation. Split it only in `require_oidc()`. Keep issuer/audience optional on the shared settings model so workers and Fake tests remain independent of production authentication:

```python
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator

_ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {
        "RS256", "RS384", "RS512",
        "PS256", "PS384", "PS512",
        "ES256", "ES384", "ES512",
        "EdDSA",
    }
)

auth_issuer: str | None = None
auth_audience: str | None = None
auth_allowed_algorithms: str = "RS256"

@model_validator(mode="after")
def validate_auth_settings(self) -> Self:
    algorithms = tuple(
        item.strip() for item in self.auth_allowed_algorithms.split(",")
    )
    if not algorithms or any(
        item not in _ASYMMETRIC_JWT_ALGORITHMS
        for item in algorithms
    ):
        raise ValueError("JWT algorithm allowlistには対応する非対称方式だけを指定します")
    if self.auth_issuer is not None:
        parsed = urlsplit(self.auth_issuer)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("AIRPG_AUTH_ISSUERにはquery/fragmentのないHTTPS URLが必要です")
    return self

def require_oidc(self) -> tuple[str, str, tuple[str, ...]]:
    if self.auth_issuer is None:
        raise ValueError("AIRPG_AUTH_ISSUER is required for production API authentication")
    if self.auth_audience is None or not self.auth_audience:
        raise ValueError("AIRPG_AUTH_AUDIENCE is required for production API authentication")
    algorithms = tuple(
        item.strip() for item in self.auth_allowed_algorithms.split(",")
    )
    return self.auth_issuer, self.auth_audience, algorithms
```

Merge this validation into the existing timing validator rather than defining two methods with the same name.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_auth.py tests\unit\test_config.py -q -p no:cacheprovider
```

Expected: all focused tests pass.

- [ ] **Step 7: Commit the contract and settings**

```powershell
git add src/ai_rpg/application/auth.py src/ai_rpg/config.py tests/unit/test_auth.py tests/unit/test_config.py
git commit -m "feat: define oidc authentication settings"
```

---

### Task 2: Immutable principal identity persistence

**Files:**
- Create: `migrations/versions/0007_oidc_identities.py`
- Create: `src/ai_rpg/infrastructure/postgres/identities.py`
- Modify: `src/ai_rpg/infrastructure/postgres/models.py`
- Modify: `src/ai_rpg/infrastructure/postgres/__init__.py`
- Modify: `tests/unit/test_postgres_models.py`
- Modify: `tests/integration/postgres/test_migrations.py`

**Interfaces:**
- Produces: `IdentityRecord(issuer, subject, principal_id, created_at, disabled_at)`
- Produces: `PostgresIdentityStore.resolve(issuer, subject) -> UUID | None`
- Produces: `PostgresIdentityStore.register(issuer, subject, principal_id=None) -> IdentityRecord`
- Produces: `PostgresIdentityStore.disable(issuer, subject) -> IdentityRecord`
- Produces: `IdentityRegistrationConflict` and `IdentityNotFound`
- Consumes: `create_session_factory(database_url)` and SQLAlchemy async sessions

- [ ] **Step 1: Add failing ORM metadata assertions**

Update the metadata test to include both tables and inspect the identity columns:

```python
from ai_rpg.infrastructure.postgres.models import (
    Base,
    EntityModel,
    PrincipalIdentityModel,
    TurnModel,
)

assert {"principals", "principal_identities"} <= set(Base.metadata.tables)
assert {
    "issuer",
    "subject",
    "principal_id",
    "created_at",
    "disabled_at",
} == set(PrincipalIdentityModel.__table__.columns.keys())
```

- [ ] **Step 2: Run the metadata test and observe missing models**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_postgres_models.py -q -p no:cacheprovider
```

Expected: import or table assertion fails.

- [ ] **Step 3: Add typed ORM models**

Add these mappings near `CampaignMemberModel`:

```python
class PrincipalModel(Base):
    __tablename__ = "principals"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PrincipalIdentityModel(Base):
    __tablename__ = "principal_identities"

    issuer: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principals.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(issuer)>0", name="principal_identity_issuer_nonempty"),
        CheckConstraint("length(subject)>0", name="principal_identity_subject_nonempty"),
        CheckConstraint(
            "disabled_at IS NULL OR disabled_at>=created_at",
            name="principal_identity_disabled_after_created",
        ),
        Index("principal_identities_principal", "principal_id"),
    )
```

- [ ] **Step 4: Add the Alembic migration with one-way history protection**

Create revision `0007_oidc_identities` with `down_revision = "0006_entity_refs"`. Its upgrade must create the two tables, index, and this trigger contract:

```sql
CREATE FUNCTION guard_principal_identity_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'principal identity history is immutable';
    END IF;
    IF NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.disabled_at IS NULL
       OR OLD.disabled_at IS NOT NULL THEN
        RAISE EXCEPTION 'principal identity can only be disabled once';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER guard_principal_identity
BEFORE UPDATE OR DELETE ON principal_identities
FOR EACH ROW EXECUTE FUNCTION guard_principal_identity_history();
```

The downgrade order must be: trigger, function, `principal_identities`, `principals`.

- [ ] **Step 5: Add failing PostgreSQL identity lifecycle tests**

Use the existing `database` fixture and SelectorEventLoop pattern. Cover registration, resolution, idempotency, conflict, disable, and DB mutation rejection:

```python
def test_principal_identity_lifecycle_is_pre_registered_and_immutable(
    database: Engine,
) -> None:
    from ai_rpg.infrastructure.database import create_session_factory
    from ai_rpg.infrastructure.postgres import (
        IdentityRegistrationConflict,
        PostgresIdentityStore,
    )

    principal_id = uuid4()
    store = PostgresIdentityStore(
        create_session_factory(database.url.render_as_string(hide_password=False))
    )

    async def exercise() -> tuple[object, object, object]:
        first = await store.register("https://idp.example.com/", "player-1", principal_id)
        repeated = await store.register(
            "https://idp.example.com/", "player-1", principal_id
        )
        assert await store.resolve("https://idp.example.com/", "player-1") == principal_id
        with pytest.raises(IdentityRegistrationConflict):
            await store.register(
                "https://idp.example.com/", "player-1", uuid4()
            )
        disabled = await store.disable("https://idp.example.com/", "player-1")
        assert await store.resolve("https://idp.example.com/", "player-1") is None
        return first, repeated, disabled

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        first, repeated, disabled = runner.run(exercise())

    assert first == repeated
    assert disabled.disabled_at is not None
    with pytest.raises(DBAPIError):
        with database.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM principal_identities "
                    "WHERE issuer=:issuer AND subject=:subject"
                ),
                {"issuer": "https://idp.example.com/", "subject": "player-1"},
            )
```

Also extend `test_empty_database_upgrades_and_downgrades` to assert the new tables exist after upgrade and disappear after downgrade.

- [ ] **Step 6: Implement the async identity store**

Create immutable records and explicit errors:

```python
@dataclass(frozen=True, slots=True)
class IdentityRecord:
    issuer: str
    subject: str
    principal_id: UUID
    created_at: datetime
    disabled_at: datetime | None


class IdentityRegistrationConflict(Exception):
    pass


class IdentityNotFound(Exception):
    pass
```

Implement `resolve` with a typed `select` that requires `disabled_at IS NULL`. Implement `register` in one session transaction:

1. Select the canonical identity row first. If it is active, return it when `principal_id` is omitted or matches; otherwise raise `IdentityRegistrationConflict`.
2. If no row exists, choose the supplied `principal_id` or generate one UUID.
3. Insert `PrincipalModel` with PostgreSQL `on_conflict_do_nothing`.
4. Insert `PrincipalIdentityModel` with `on_conflict_do_nothing`.
5. Select the canonical row again to resolve a concurrent insert.
6. Return it when active and, for an explicit requested ID, mapped to that ID. For an omitted ID, return the concurrent winner. Otherwise raise `IdentityRegistrationConflict` so all writes in this attempt roll back.

Implement `disable` with `SELECT ... FOR UPDATE`, return an already-disabled row unchanged, set `datetime.now(UTC)` only for an active row, and raise `IdentityNotFound` for an unknown pair.

- [ ] **Step 7: Export the identity adapter and run focused tests**

Add `IdentityNotFound`, `IdentityRecord`, `IdentityRegistrationConflict`, and `PostgresIdentityStore` to `src/ai_rpg/infrastructure/postgres/__init__.py`.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_postgres_models.py -q -p no:cacheprovider
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:55433/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "empty_database or principal_identity"
```

Expected: metadata, migration lifecycle, identity lifecycle, and immutability pass.

- [ ] **Step 8: Commit persistence**

```powershell
git add migrations/versions/0007_oidc_identities.py src/ai_rpg/infrastructure/postgres/models.py src/ai_rpg/infrastructure/postgres/identities.py src/ai_rpg/infrastructure/postgres/__init__.py tests/unit/test_postgres_models.py tests/integration/postgres/test_migrations.py
git commit -m "feat: persist pre-registered oidc identities"
```

---

### Task 3: Discovery and JWT verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/ai_rpg/api/auth.py`
- Create: `tests/unit/test_oidc_auth.py`

**Interfaces:**
- Produces: `OidcConfiguration(issuer, jwks_uri)`
- Produces: `ValidatedOidcIdentity(issuer, subject, authenticated_at, expires_at)`
- Produces: `discover_oidc(issuer, client) -> OidcConfiguration`
- Produces: `OidcJwtVerifier.verify(token) -> ValidatedOidcIdentity`
- Produces: `InvalidCredentialError`, `AuthenticationUnavailableError`, `OidcDiscoveryError`
- Consumes: `Settings.require_oidc()` from Task 1

- [ ] **Step 1: Add and lock PyJWT crypto support**

Add this single dependency to `[project].dependencies`:

```toml
"pyjwt[crypto]>=2.14,<3",
```

Update the lock:

```powershell
.\.venv\Scripts\uv.exe lock
.\.venv\Scripts\uv.exe sync --frozen
```

Expected: `uv.lock` contains `pyjwt` and `cryptography`, and sync succeeds.

- [ ] **Step 2: Add failing Discovery tests**

Use `httpx.MockTransport` to test exact issuer matching, HTTPS JWKS enforcement, malformed data, and timeout mapping:

```python
@pytest.mark.asyncio
async def test_discovery_requires_exact_issuer_and_https_jwks() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://idp.example.com/.well-known/openid-configuration"
        )
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.example.com/",
                "jwks_uri": "https://idp.example.com/keys",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        configuration = await discover_oidc("https://idp.example.com/", client)

    assert configuration.jwks_uri == "https://idp.example.com/keys"
```

Add separate tests returning a different issuer, `http://` JWKS, non-object JSON, missing fields, HTTP 500, and `httpx.ReadTimeout`; each must raise `OidcDiscoveryError` without including response bodies or token data.

- [ ] **Step 3: Implement strict startup Discovery**

Create `OidcConfiguration` and `discover_oidc`:

```python
@dataclass(frozen=True, slots=True)
class OidcConfiguration:
    issuer: str
    jwks_uri: str


class OidcDiscoveryError(RuntimeError):
    pass


async def discover_oidc(
    issuer: str,
    client: httpx.AsyncClient,
) -> OidcConfiguration:
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise OidcDiscoveryError("OIDC Discoveryを取得できません") from error
    if not isinstance(payload, dict) or payload.get("issuer") != issuer:
        raise OidcDiscoveryError("OIDC Discoveryのissuerが一致しません")
    jwks_uri = payload.get("jwks_uri")
    parsed = urlsplit(jwks_uri) if isinstance(jwks_uri, str) else None
    if parsed is None or parsed.scheme != "https" or not parsed.netloc:
        raise OidcDiscoveryError("OIDC Discoveryのjwks_uriが不正です")
    return OidcConfiguration(issuer=issuer, jwks_uri=jwks_uri)
```

- [ ] **Step 4: Add failing JWT verification tests**

Generate a test RSA key with `cryptography`, assign a `kid`, and create tokens with `jwt.encode`. Inject a real `jwt.PyJWKClient` whose `fetch_data` method returns the in-memory JWKS. Cover:

- valid RS256 token returns only issuer, subject, authentication time, and expiry;
- bad signature, wrong issuer/audience, missing required claim, expired token, future `nbf`, invalid `iat`, and disallowed algorithm raise `InvalidCredentialError`;
- `PyJWKClientConnectionError` raises `AuthenticationUnavailableError`;
- unknown `kid` that is absent after refresh raises `InvalidCredentialError`;
- repeated known `kid` uses the JWKS cache;
- a rotated key succeeds after the client refresh path.

Use this shape for the valid case:

```python
identity = await verifier.verify(token)

assert identity.issuer == "https://idp.example.com/"
assert identity.subject == "player-1"
assert identity.expires_at > identity.authenticated_at
assert not hasattr(identity, "email")
```

- [ ] **Step 5: Implement the verifier with fixed algorithms and worker-thread JWKS calls**

Define transport-local values and errors:

```python
@dataclass(frozen=True, slots=True)
class ValidatedOidcIdentity:
    issuer: str
    subject: str
    authenticated_at: datetime
    expires_at: datetime


class InvalidCredentialError(Exception):
    pass


class AuthenticationUnavailableError(RuntimeError):
    pass
```

`OidcJwtVerifier` receives `issuer`, `audience`, `algorithms`, and `jwt.PyJWKClient`. Its `verify` method must:

```python
authenticated_at = datetime.now(UTC)
try:
    signing_key = await asyncio.to_thread(
        self._jwks.get_signing_key_from_jwt,
        token,
    )
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=list(self._algorithms),
        audience=self._audience,
        issuer=self._issuer,
        leeway=30,
        options={"require": ["iss", "aud", "sub", "exp"]},
    )
except jwt.PyJWKClientConnectionError as error:
    raise AuthenticationUnavailableError("JWKSを取得できません") from error
except (jwt.InvalidTokenError, jwt.PyJWKClientError) as error:
    raise InvalidCredentialError("Bearer tokenが不正です") from error
```

After decode, require a non-empty string `sub`, reject boolean/non-numeric `exp`, convert `exp` with `datetime.fromtimestamp(value, UTC)`, and reject `expires_at <= authenticated_at`. Return no other claims.

- [ ] **Step 6: Run OIDC unit tests and static checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_oidc_auth.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check src\ai_rpg\api\auth.py tests\unit\test_oidc_auth.py
.\.venv\Scripts\python.exe -m mypy src
```

Expected: unit tests, Ruff, and mypy pass.

- [ ] **Step 7: Commit verification boundary**

```powershell
git add pyproject.toml uv.lock src/ai_rpg/api/auth.py tests/unit/test_oidc_auth.py
git commit -m "feat: verify oidc bearer jwt"
```

---

### Task 4: FastAPI authentication mapping and SSE expiry

**Files:**
- Modify: `src/ai_rpg/api/auth.py`
- Modify: `src/ai_rpg/api/app.py`
- Modify: `src/ai_rpg/api/__init__.py`
- Modify: `tests/unit/test_oidc_auth.py`
- Modify: `tests/integration/test_api.py`

**Interfaces:**
- Produces: `OidcBearerAuthenticator.__call__(authorization) -> AuthenticatedPrincipal`
- Produces: `build_oidc_authenticator(settings) -> OidcBearerAuthenticator`
- Consumes: `PostgresIdentityStore.resolve`, `OidcJwtVerifier.verify`, `Settings.require_oidc()`
- Consumes: existing `create_app(principal_provider=...)`

- [ ] **Step 1: Add failing Bearer dependency tests**

Construct `OidcBearerAuthenticator` with an `AsyncMock` verifier and resolver callable. Assert:

```python
principal = await authenticator("Bearer signed-token")

assert principal.principal_id == principal_id
assert principal.auth_context == frozenset()
assert principal.credential_expires_at == validated.expires_at
resolver.assert_awaited_once_with(validated.issuer, validated.subject)
```

Parameterize missing header, empty token, Basic auth, invalid token, unknown identity, and disabled identity to expect:

```python
assert error.value.status_code == 401
assert error.value.detail == {"code": "UNAUTHENTICATED"}
assert error.value.headers == {"WWW-Authenticate": "Bearer"}
```

Assert `AuthenticationUnavailableError` and a resolver `SQLAlchemyError` map to 503 with `AUTHENTICATION_UNAVAILABLE`. Use `caplog` to verify logs contain the event classification but not token or subject.

- [ ] **Step 2: Implement strict header extraction and identity mapping**

Use an awaitable resolver callable rather than exposing a broad repository port:

```python
IdentityResolver = Callable[[str, str], Awaitable[UUID | None]]


class OidcBearerAuthenticator:
    def __init__(
        self,
        verifier: OidcJwtVerifier,
        resolve_identity: IdentityResolver,
    ) -> None:
        self._verifier = verifier
        self._resolve_identity = resolve_identity

    async def __call__(
        self,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedPrincipal:
        token = _bearer_token(authorization)
        validated = await self._verify_or_http_error(token)
        try:
            principal_id = await self._resolve_identity(
                validated.issuer,
                validated.subject,
            )
        except SQLAlchemyError as error:
            logger.warning("oidc_identity_store_unavailable")
            raise _authentication_unavailable() from error
        if principal_id is None:
            raise _unauthenticated()
        return AuthenticatedPrincipal(
            principal_id=principal_id,
            issuer=validated.issuer,
            subject=validated.subject,
            authenticated_at=validated.authenticated_at,
            auth_context=frozenset(),
            credential_expires_at=validated.expires_at,
        )
```

`_bearer_token` must require exactly two whitespace-separated parts and compare scheme case-insensitively. `_unauthenticated()` must always return the same 401 detail/header. Log fixed event names such as `oidc_authentication_succeeded`, `oidc_credential_rejected`, `oidc_identity_unavailable`, and `oidc_provider_unavailable`; never interpolate token or subject. The success log may include only the validated Issuer and internal principal ID.

- [ ] **Step 3: Add failing API 401-header and SSE-expiry tests**

Extend the unconfigured authenticator test:

```python
assert response.headers["www-authenticate"] == "Bearer"
```

Add a deterministic SSE expiry test by injecting a UTC clock into `create_app`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_sse_closes_when_authenticated_credential_expires() -> None:
    authenticated_at = datetime(2026, 9, 18, tzinfo=UTC)
    expires_at = authenticated_at + timedelta(seconds=1)
    principal = replace(
        _principal(),
        authenticated_at=authenticated_at,
        credential_expires_at=expires_at,
    )

    async def authenticated() -> AuthenticatedPrincipal:
        return principal

    event_stream = AsyncMock()
    event_stream.poll.return_value = ()
    app = create_app(
        turn_service=AsyncMock(),
        turn_query_service=AsyncMock(),
        event_stream_service=event_stream,
        principal_provider=authenticated,
        event_poll_seconds=0,
        utc_now=lambda: expires_at,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/campaigns/{CAMPAIGN_ID}/events")

    assert response.status_code == 200
    assert response.text == ""
    assert event_stream.poll.await_count == 1
```

- [ ] **Step 4: Make principal dependency typing and SSE expiry explicit**

Change `PrincipalProvider` to:

```python
PrincipalProvider = Callable[..., Awaitable[AuthenticatedPrincipal]]
```

Give `_unconfigured_principal` the same `WWW-Authenticate` header as OIDC failures. Add `utc_now: Callable[[], datetime] = lambda: datetime.now(UTC)` to `create_app`. In the SSE body, check expiry at the top of every loop and immediately before emitting each event:

```python
def credential_expired() -> bool:
    expires_at = principal.credential_expires_at
    return expires_at is not None and utc_now() >= expires_at
```

Return from the stream when it becomes true. Do not move this check into Application services; it belongs to the live HTTP credential.

- [ ] **Step 5: Add production builder**

In `api/auth.py`, create `build_oidc_authenticator(settings)` that:

1. Calls `settings.require_oidc()`.
2. Uses `httpx.AsyncClient(timeout=5.0)` for `discover_oidc`.
3. Creates `jwt.PyJWKClient(jwks_uri, lifespan=300, timeout=5, cooldown_duration=30)`.
4. Creates `PostgresIdentityStore(create_session_factory(settings.database_url))`.
5. Returns `OidcBearerAuthenticator(verifier, store.resolve)`.

Export `build_oidc_authenticator` and `OidcDiscoveryError` from `src/ai_rpg/api/__init__.py` so the CLI imports a stable API boundary.

- [ ] **Step 6: Run HTTP and SSE focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_oidc_auth.py tests\integration\test_api.py -q -p no:cacheprovider
```

Expected: Bearer mapping, unified errors, existing API behavior, membership revocation, and credential expiry pass.

- [ ] **Step 7: Commit HTTP integration**

```powershell
git add src/ai_rpg/api/auth.py src/ai_rpg/api/app.py src/ai_rpg/api/__init__.py tests/unit/test_oidc_auth.py tests/integration/test_api.py
git commit -m "feat: authenticate api bearer tokens"
```

---

### Task 5: Administration CLI and production API startup

**Files:**
- Modify: `src/ai_rpg/cli.py`
- Create: `tests/unit/test_cli.py`
- Modify: `tests/integration/postgres/test_migrations.py`

**Interfaces:**
- Produces: `ai-rpg auth register --subject VALUE [--principal-id UUID]`
- Produces: `ai-rpg auth disable --subject VALUE`
- Consumes: `PostgresIdentityStore`, `Settings.auth_issuer`, `build_oidc_authenticator`

- [ ] **Step 1: Add failing parser and command tests**

Test exact parser results without starting external processes:

```python
def test_auth_register_parser_accepts_optional_principal_id() -> None:
    principal_id = uuid4()
    args = _parser().parse_args(
        [
            "auth",
            "register",
            "--subject",
            "player-1",
            "--principal-id",
            str(principal_id),
        ]
    )

    assert args.command == "auth"
    assert args.auth_command == "register"
    assert args.subject == "player-1"
    assert args.principal_id == principal_id
```

Add AsyncMock-backed tests for register/disable JSON output and errors. Assert the JSON contains issuer, subject, principal ID, and status but no token field. Add a test that `api` without `--dev-principal` awaits `build_oidc_authenticator`, while `api --dev-principal` does not call it.

- [ ] **Step 2: Add nested auth subcommands**

Extend `_parser()` with:

```python
auth = commands.add_parser("auth")
auth_commands = auth.add_subparsers(dest="auth_command", required=True)

register = auth_commands.add_parser("register")
register.add_argument("--subject", required=True)
register.add_argument("--principal-id", type=UUID)

disable = auth_commands.add_parser("disable")
disable.add_argument("--subject", required=True)
```

Reject empty/whitespace subjects in the command handler before opening a transaction.

- [ ] **Step 3: Implement small async command handlers**

Add handlers that use only the configured Issuer:

```python
async def _run_auth_command(
    args: argparse.Namespace,
    settings: Settings,
) -> dict[str, object]:
    if settings.auth_issuer is None:
        raise ValueError("AIRPG_AUTH_ISSUER is required for identity administration")
    subject = args.subject.strip()
    if not subject:
        raise ValueError("subjectは空にできません")
    store = PostgresIdentityStore(create_session_factory(settings.database_url))
    if args.auth_command == "register":
        record = await store.register(
            settings.auth_issuer,
            subject,
            args.principal_id,
        )
        status_value = "active"
    else:
        record = await store.disable(settings.auth_issuer, subject)
        status_value = "disabled"
    return {
        "issuer": record.issuer,
        "subject": record.subject,
        "principal_id": str(record.principal_id),
        "status": status_value,
    }
```

In `main`, call it with `asyncio.run`, print `json.dumps(..., ensure_ascii=False)`, and convert `ValueError`, `IdentityNotFound`, and `IdentityRegistrationConflict` to `parser.error(str(error))`.

- [ ] **Step 4: Wire production API startup**

Change only the non-development branch:

```python
if args.dev_principal is not None:
    app = create_app(principal_provider=_development_principal(args.dev_principal))
else:
    try:
        principal_provider = asyncio.run(build_oidc_authenticator(settings))
    except (ValueError, OidcDiscoveryError) as error:
        parser.error(str(error))
    app = create_app(principal_provider=principal_provider)
```

Keep `/health` unprotected, keep static files unchanged, and do not build OIDC authentication for worker commands.

- [ ] **Step 5: Add real PostgreSQL CLI lifecycle coverage**

In `test_migrations.py`, start from the migrated test DB and run subprocess commands with `AIRPG_AUTH_ISSUER=https://idp.example.com/`:

1. `auth register --subject player-1 --principal-id PRINCIPAL_A` returns active JSON.
2. Repeating it returns the same principal.
3. `auth disable --subject player-1` returns disabled JSON.
4. Repeating disable remains successful and preserves the first `disabled_at` in DB.
5. Registering the disabled pair fails without changing its row.

Never pass a token or secret in the subprocess arguments.

- [ ] **Step 6: Run CLI tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_cli.py -q -p no:cacheprovider
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:55433/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres\test_migrations.py -q -p no:cacheprovider -k "identity_cli"
```

Expected: parser, JSON contract, startup branch, and DB lifecycle pass.

- [ ] **Step 7: Commit CLI and runtime wiring**

```powershell
git add src/ai_rpg/cli.py tests/unit/test_cli.py tests/integration/postgres/test_migrations.py
git commit -m "feat: administer oidc identities from cli"
```

---

### Task 6: Documentation, complete verification, and remote checkpoint

**Files:**
- Modify: `README.md`
- Modify: `docs/adr/0010-authenticated-principal.md`
- Modify: `tests/integration/postgres/test_migrations.py`

**Interfaces:**
- Documents: exact environment variables, CLI commands, HTTP status semantics, development bypass, and future login boundary
- Verifies: every interface produced by Tasks 1-5

- [ ] **Step 1: Update ADR-0010 to match the implemented contract**

Update the shown dataclass with `credential_expires_at`. Add decisions stating:

- production API uses one configured OIDC Issuer and Bearer JWT;
- `(issuer, subject)` must be pre-registered and maps to stable `principal_id`;
- identity rows can be disabled but not reassigned, deleted, or re-enabled in this release;
- Campaign authorization remains DB-backed and token role claims are ignored;
- SSE closes at credential expiry and continues to recheck Campaign membership;
- a future browser login/session adapter must produce the same principal contract.

- [ ] **Step 2: Update README production instructions**

Replace the statement that production authentication is not connected. Add a section containing these exact operator examples:

```powershell
$env:AIRPG_AUTH_ISSUER = "https://idp.example.com/"
$env:AIRPG_AUTH_AUDIENCE = "ai-rpg-api"
$env:AIRPG_AUTH_ALLOWED_ALGORITHMS = "RS256"

.\.venv\Scripts\ai-rpg.exe auth register `
  --subject "oidc-subject-from-provider" `
  --principal-id "00000000-0000-0000-0000-000000000021"

.\.venv\Scripts\ai-rpg.exe api --host 127.0.0.1 --port 8000
```

Document authenticated requests with `Authorization: Bearer <token>`, the disable command, 401/403/503 meanings, migration prerequisite, JWKS rotation behavior, and that `--dev-principal` bypasses OIDC only for local development. Mark “本番認証adapter” complete in the roadmap. State explicitly that browser login is a future adapter and the bundled play screen still relies on development principal until that work is implemented.

- [ ] **Step 3: Run documentation and dependency checks**

```powershell
git diff --check
.\.venv\Scripts\uv.exe lock --check --offline
.\.venv\Scripts\uv.exe build --offline
```

Expected: no whitespace errors, lock is current, and wheel/sdist build succeeds.

- [ ] **Step 4: Run all non-PostgreSQL checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit tests\contract tests\integration\test_api.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
node tests\browser\play_state.test.cjs
node tests\browser\play_screen.test.cjs
```

Expected: all Python tests pass; Ruff and mypy report success; both Node test files report zero failures.

- [ ] **Step 5: Run the complete PostgreSQL suite on a dedicated empty test DB**

Start or reuse only a disposable DB whose name begins with `ai_rpg_test`, then run:

```powershell
$env:AIRPG_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:55433/ai_rpg_test_local"
.\.venv\Scripts\python.exe -m pytest tests\integration\postgres -q -p no:cacheprovider
```

Expected: the complete suite passes, including upgrade/downgrade, immutable identity history, CLI lifecycle, existing worker recovery, and Fake round trips. Stop the dedicated container after verification.

- [ ] **Step 6: Run a local mock-OIDC acceptance path**

Add `test_oidc_bearer_round_trip_uses_registered_identity_and_campaign_membership` to `tests/integration/postgres/test_migrations.py`. Use `httpx.MockTransport` for Discovery, patch the injected `PyJWKClient.fetch_data` with an in-memory JWKS, register through `PostgresIdentityStore`, and exercise this connected path:

```text
Discovery -> JWKS -> signed Bearer token -> registered identity
-> FastAPI dependency -> existing Campaign membership -> GET state
```

Verify all of the following in the same acceptance path:

- a valid registered token receives 200;
- the same token with an unregistered subject receives 401;
- disabling the identity makes the next request receive 401;
- a registered principal without Campaign membership receives 403;
- no token text appears in captured logs.

Run the exact test by node ID and record its passing output in the execution notes.

- [ ] **Step 7: Commit docs and any acceptance-test adjustment**

```powershell
git add README.md docs/adr/0010-authenticated-principal.md tests/integration/postgres/test_migrations.py
git commit -m "docs: document production oidc authentication"
```

The mock-OIDC acceptance test is required in this task, so stage that exact test file rather than the whole `tests` tree.

- [ ] **Step 8: Verify clean state, push main, and verify remote SHA**

```powershell
git status -sb
git log -6 --oneline
git push origin main
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

Expected: the worktree is clean before push, push succeeds, and local `HEAD` exactly equals the remote `refs/heads/main` SHA. Report test counts, commit SHAs, remote SHA, and the remaining limitation that browser login is not yet implemented.
