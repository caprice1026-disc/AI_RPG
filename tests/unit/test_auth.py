"""認証済みprincipalのtransport非依存契約。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from ai_rpg.application import AuthenticatedPrincipal


def test_authenticated_at_must_be_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        AuthenticatedPrincipal(
            principal_id=uuid4(),
            issuer="test",
            subject="player",
            authenticated_at=datetime.now(timezone(timedelta(hours=9))),
            auth_context=frozenset(),
        )


def test_authenticated_principal_accepts_aware_utc() -> None:
    principal = AuthenticatedPrincipal(
        principal_id=uuid4(),
        issuer="test",
        subject="player",
        authenticated_at=datetime.now(UTC),
        auth_context=frozenset({"mfa"}),
    )

    assert principal.auth_context == frozenset({"mfa"})


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
    with pytest.raises(ValueError, match=r"credential_expires_at.*UTC"):
        AuthenticatedPrincipal(
            principal_id=uuid4(),
            issuer="test",
            subject="player",
            authenticated_at=datetime.now(UTC),
            auth_context=frozenset(),
            credential_expires_at=datetime.now(timezone(timedelta(hours=9))),
        )
