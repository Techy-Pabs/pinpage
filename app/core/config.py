from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Read from environment variables first, then from a .env file if present.
    # extra="ignore" skips variables we don't use yet (e.g. POSTGRES_PASSWORD,
    # or the R2 and limit settings, which later slices start reading).
    # hide_input_in_errors keeps values (which include secrets) out of the
    # error message when a setting is missing or invalid.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Required: the app refuses to start without them
    database_url: str
    jwt_secret: str

    # How long a login token stays valid
    jwt_expire_minutes: int = 30

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


settings = Settings()
