"""環境変数から実行時設定を読み込む単一境界。"""

from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class Settings(BaseSettings):
    """検証済みの実行時設定を提供する。"""

    model_config = SettingsConfigDict(
        env_prefix="AIRPG_", env_file=".env", extra="ignore", frozen=True,
        populate_by_name=True, hide_input_in_errors=True,
    )

    max_actions_per_turn: int = Field(default=3, ge=1, le=10)
    worker_lease_seconds: int = Field(default=60, ge=30, le=300)
    resolution_max_attempts: int = Field(default=3, ge=1, le=10)
    narration_max_attempts: int = Field(default=3, ge=1, le=10)
    resolution_deadline_seconds: int = Field(default=120, ge=60, le=900)
    narration_deadline_seconds: int = Field(default=120, ge=60, le=900)
    recent_messages_limit: int = Field(default=20, ge=0, le=100)
    llm_timeout_seconds: int = Field(default=30, ge=5, le=120)
    narrative_call_budget: int = Field(default=1, ge=1, le=1)
    mechanical_call_budget: int = Field(default=3, ge=3, le=3)
    llm_model: str = Field(default="google:gemini-3.5-flash", min_length=1)
    fast_model: str = Field(default="google:gemini-3.5-flash", min_length=1)
    quality_model: str = Field(default="google:gemini-3.5-flash", min_length=1)
    background_model: str = Field(default="google:gemini-3.5-flash", min_length=1)
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("AIRPG_GEMINI_API_KEY", "GEMINI_API_KEY")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("AIRPG_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    database_url: str = Field(
        default="postgresql+psycopg://airpg:airpg@localhost/airpg", min_length=1
    )
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_allowed_algorithms: str = "RS256"

    @model_validator(mode="before")
    @classmethod
    def inherit_model_ids(cls, values: dict[str, object]) -> dict[str, object]:
        values = dict(values)
        common = values.get("llm_model", cls.model_fields["llm_model"].default)
        for tier in ("fast_model", "quality_model", "background_model"):
            if values.get(tier) is None:
                values[tier] = common
        return values

    @field_validator("llm_model", "fast_model", "quality_model", "background_model")
    @classmethod
    def normalize_model_id(cls, value: str) -> str:
        """Bare IDs and openai: are compatibility aliases for openai-responses:."""

        value = value.strip()
        provider, separator, model = value.partition(":")
        if not separator:
            provider, model = "openai-responses", value
        if provider == "openai":
            provider = "openai-responses"
        if provider not in {"google", "openai-responses"} or not model.strip():
            raise ValueError("LLM model requires google: or openai-responses: and a model name")
        return f"{provider}:{model.strip()}"

    @model_validator(mode="after")
    def validate_worker_timing(self) -> Self:
        if self.llm_timeout_seconds >= self.worker_lease_seconds:
            raise ValueError("LLM timeoutはworker leaseより短くする必要があります")
        if self.resolution_deadline_seconds < self.worker_lease_seconds:
            raise ValueError("resolution deadlineはworker lease以上である必要があります")
        if self.narration_deadline_seconds < self.worker_lease_seconds:
            raise ValueError("narration deadlineはworker lease以上である必要があります")
        algorithms = tuple(item.strip() for item in self.auth_allowed_algorithms.split(","))
        if not algorithms or any(
            item not in _ASYMMETRIC_JWT_ALGORITHMS for item in algorithms
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
        algorithms = tuple(item.strip() for item in self.auth_allowed_algorithms.split(","))
        return self.auth_issuer, self.auth_audience, algorithms


@lru_cache
def get_settings() -> Settings:
    """プロセス内で共有する設定を一度だけ読み込む。"""

    return Settings()
