import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# An R2 account id goes into the endpoint's host name, so only letters,
# digits and hyphens are allowed (no dots, slashes or "@" to redirect it)
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


class Settings(BaseSettings):
    # Read from environment variables first, then from a .env file if present.
    # extra="ignore" skips variables we don't use yet (e.g. POSTGRES_PASSWORD,
    # or MAX_PAGES, which Slice 4 starts reading).
    # hide_input_in_errors keeps values (which include secrets) out of the
    # error message when a setting is missing or invalid.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Required: the app refuses to start without them
    database_url: str
    jwt_secret: str

    # How long a login token stays valid
    jwt_expire_minutes: int = 30

    # Cloudflare R2, where the original PDFs are stored. All required.
    r2_account_id: str
    r2_access_key_id: str
    # SecretStr prints as "**********" in logs, errors and repr(settings);
    # the real value is only read where the R2 client is created
    r2_secret_access_key: SecretStr
    r2_bucket: str

    # How long a signed link to an original PDF stays valid (at most 7 days,
    # the longest a signed S3/R2 link can last)
    signed_url_seconds: int = Field(default=300, gt=0, le=7 * 24 * 3600)

    # Upload limits. An upload is held in memory while it is checked, so the
    # file size is capped at 100 MB whatever the setting says.
    max_upload_mb: int = Field(default=20, gt=0, le=100)
    # Only processing and ready documents count; failed ones don't
    max_documents_per_workspace: int = Field(default=10, gt=0)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, url: str) -> str:
        if not url.strip():
            # e.g. an empty shell variable passed with "-e DATABASE_URL"
            raise ValueError("DATABASE_URL is empty")
        # Hosts such as Neon give URLs starting with postgresql:// (or postgres://).
        # SQLAlchemy reads that as "use psycopg2", which isn't installed, so switch
        # it to psycopg (version 3). Options such as ?sslmode=require are kept and
        # passed on to the database driver unchanged.
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url.removeprefix(prefix)
        return url

    @field_validator("jwt_secret")
    @classmethod
    def jwt_secret_not_empty(cls, value: str) -> str:
        # An empty secret would let anyone sign their own tokens
        if not value.strip():
            raise ValueError("JWT_SECRET is empty")
        return value

    @field_validator("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
    @classmethod
    def r2_value_not_empty(cls, value, info):
        # The message names the setting, never its value
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw.strip():
            raise ValueError(f"{info.field_name.upper()} is empty")
        return value

    @field_validator("r2_account_id")
    @classmethod
    def r2_account_id_is_safe(cls, value: str) -> str:
        if not ACCOUNT_ID_PATTERN.fullmatch(value):
            raise ValueError("R2_ACCOUNT_ID may only contain letters, digits and hyphens")
        return value


settings = Settings()
