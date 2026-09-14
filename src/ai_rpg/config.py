"""環境変数から実行時設定を読み込む単一境界。"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """検証済みの実行時設定を提供する。"""

    model_config = SettingsConfigDict(
        env_prefix="AIRPG_", env_file=".env", extra="ignore", frozen=True
    )

    max_actions_per_turn: int = Field(default=3, ge=1, le=10)
    worker_lease_seconds: int = Field(default=60, ge=30, le=300)
    recent_messages_limit: int = Field(default=20, ge=0, le=100)
    llm_timeout_seconds: int = Field(default=30, ge=5, le=120)
    narrative_call_budget: int = Field(default=1, ge=1, le=1)
    mechanical_call_budget: int = Field(default=3, ge=3, le=3)
    fast_model: str = Field(default="gpt-5-mini", min_length=1)
    quality_model: str = Field(default="gpt-5.4", min_length=1)
    background_model: str = Field(default="gpt-5-mini", min_length=1)
    database_url: str = Field(
        default="postgresql+psycopg://airpg:airpg@localhost/airpg", min_length=1
    )


@lru_cache
def get_settings() -> Settings:
    """プロセス内で共有する設定を一度だけ読み込む。"""

    return Settings()
