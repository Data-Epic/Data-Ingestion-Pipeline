"""
Centralized configuration, loaded from environment variables (.env).

Why this file exists on its own:
- Every other module (client, db, api) should import settings from HERE,
  never call os.getenv() directly. That's what makes "never leak
  GITHUB_TOKEN or DB credentials" easy to guarantee later - there's one
  chokepoint to audit.

TODO (next pass):
- Define a Settings class (pydantic-settings BaseSettings is a good fit)
  with fields: github_token: str | None, database_url: str,
  request_timeout_connect: float, request_timeout_read: float,
  max_retries: int, backoff_base_seconds: float
- Load from .env via python-dotenv or pydantic-settings' built-in support
- Raise a clear error at startup if database_url is missing (fail fast,
  don't let a bad config surface later as a cryptic DB error)
"""


from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional - client.py logs a warning and proceeds unauthenticated
    # if this is unset, per the spec.
    github_token: str | None = None

    # Required - app should fail fast at startup if this is missing,
    # rather than surface a cryptic error later on first query.
    database_url: str

    request_connect_timeout: float = 5.0
    request_read_timeout: float = 15.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    """
    Cached so .env is only parsed once per process, and so every module
    that calls get_settings() shares the exact same values.
    """
    return Settings() # type: ignore[call-arg]