import re

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# An R2 account id goes into the endpoint's host name, so only letters,
# digits and hyphens are allowed (no dots, slashes or "@" to redirect it)
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


class Settings(BaseSettings):
    # Read from environment variables first, then from a .env file if present.
    # extra="ignore" skips variables the app doesn't use (e.g. POSTGRES_PASSWORD,
    # which only the database container reads).
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

    # Processing limits. A longer document fails with too_many_pages; one still
    # processing after the timeout is marked interrupted when the app starts.
    max_pages: int = Field(default=100, gt=0)
    processing_timeout_minutes: int = Field(default=15, gt=0)

    # Chunking: about this many characters per chunk, with this much
    # repeated between neighbours so a sentence cut at a boundary isn't lost
    chunk_size: int = Field(default=1000, ge=200, le=8000)
    chunk_overlap: int = Field(default=150, ge=0)

    # The AI, through OpenRouter (settings and values as in ShopAssist).
    # The key is optional so the app and tests start without it; processing
    # then fails with ai_unavailable until it is set.
    openrouter_api_key: SecretStr | None = None
    ai_base_url: str = "https://openrouter.ai/api/v1"
    # A specific free chat model. Avoid openrouter/free: it can route chat to
    # a safety classifier that answers "User Safety: safe" instead of replying.
    ai_chat_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    # Tried in order if the chat model can't answer (comma separated)
    ai_chat_fallback_models: str = "nvidia/nemotron-3.5-super-vl:free"
    ai_embedding_model: str = "nvidia/nemotron-3-embed-1b:free"
    # Must match the document_chunks.embedding column, fixed by the migration
    embedding_dimensions: int = 2048
    # Long enough for a model that thinks before it answers, and for
    # OpenRouter to try the fallback if it can't
    ai_timeout_seconds: float = Field(default=45, gt=0, le=120)
    ai_max_tokens: int = Field(default=1000, gt=0)
    # Texts per embedding request; each request counts once against the
    # free daily allowance
    embedding_batch_size: int = Field(default=64, gt=0, le=256)
    # Safely under OpenRouter's 20 a minute for free models
    ai_requests_per_minute: int = Field(default=15, gt=0, le=20)

    # Answering questions: at most this many chunks of the document per
    # question, each at least this similar (cosine similarity, -1 to 1).
    # 0.30 is ShopAssist's tuned value; tuned for Pinpage in Milestone 4.
    retrieval_top_k: int = Field(default=6, gt=0, le=20)
    retrieval_min_similarity: float = Field(default=0.30, ge=-1, le=1)
    # Questions per user per day (UTC), to protect the shared free allowance
    daily_questions_per_user: int = Field(default=20, gt=0)

    # Page questions: at most this many pages per question
    max_pages_per_question: int = Field(default=3, gt=0, le=10)
    # Section summaries: characters per summary part, and at most this many
    # parts (so one summary uses at most SUMMARY_MAX_PARTS + 1 requests)
    summary_part_chars: int = Field(default=12000, ge=2000, le=50000)
    summary_max_parts: int = Field(default=4, gt=0, le=10)

    # LangSmith (which comes with LangChain's text splitter) sends traces,
    # including document text, to LangChain's servers if any of these is
    # true. Pinpage must never do that, so the app refuses to start instead.
    # Read here only to be checked; nothing else uses them.
    langchain_tracing_v2: bool = False
    langsmith_tracing: bool = False
    langsmith_tracing_v2: bool = False
    langchain_tracing: bool = False

    @field_validator("langchain_tracing_v2", "langsmith_tracing", "langsmith_tracing_v2", "langchain_tracing")
    @classmethod
    def tracing_never_enabled(cls, value: bool, info) -> bool:
        if value:
            raise ValueError(
                f"{info.field_name.upper()} is true; LangSmith tracing would send document text "
                "outside Pinpage. Remove it or set it to false."
            )
        return value

    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def empty_key_means_none(cls, value):
        # OPENROUTER_API_KEY= (empty) counts as not set
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ai_base_url")
    @classmethod
    def ai_base_url_is_https(cls, value: str) -> str:
        # The key is sent there, so never over plain HTTP
        if not value.startswith("https://"):
            raise ValueError("AI_BASE_URL must start with https://")
        return value.rstrip("/")

    @field_validator("embedding_dimensions")
    @classmethod
    def dimensions_match_the_column(cls, value: int) -> int:
        from app.models.chunk import EMBEDDING_COLUMN_DIMENSIONS

        if value != EMBEDDING_COLUMN_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be {EMBEDDING_COLUMN_DIMENSIONS}, the size of the "
                "embedding column; a model with another size needs a new migration"
            )
        return value

    @property
    def ai_chat_models(self) -> list[str]:
        """The chat model first, then each fallback, without repeats."""
        models = [self.ai_chat_model] + [m.strip() for m in self.ai_chat_fallback_models.split(",") if m.strip()]
        return list(dict.fromkeys(models))

    @model_validator(mode="after")
    def overlap_smaller_than_chunk(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        return self

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
