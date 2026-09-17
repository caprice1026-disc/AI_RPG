"""transportやIdPから独立した認証済みrequest主体。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    principal_id: UUID
    issuer: str
    subject: str
    authenticated_at: datetime
    auth_context: frozenset[str]

    def __post_init__(self) -> None:
        if self.authenticated_at.tzinfo is None:
            raise ValueError("authenticated_atはtimezone-awareである必要があります")
        if self.authenticated_at.utcoffset() != timedelta(0):
            raise ValueError("authenticated_atはUTCである必要があります")
        if not self.issuer or not self.subject:
            raise ValueError("issuerとsubjectは空にできません")
