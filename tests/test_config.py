import pytest
from pydantic import ValidationError

from app.core.config import Settings

R2_VARIABLES = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]

# A complete, valid set of settings. Tests remove or change one value at a time.
VALID = {
    "database_url": "postgresql://u:p@h/db",
    "jwt_secret": "test",
    "r2_account_id": "abc123",
    "r2_access_key_id": "access-key-id",
    "r2_secret_access_key": "secret-access-key",
    "r2_bucket": "pinpage-test",
}


@pytest.fixture(autouse=True)
def no_settings_from_environment(monkeypatch):
    """Each test sees only the values it passes, not the container's environment."""
    for name in [
        "DATABASE_URL", "JWT_SECRET", "JWT_EXPIRE_MINUTES", "SIGNED_URL_SECONDS",
        "MAX_UPLOAD_MB", "MAX_DOCUMENTS_PER_WORKSPACE", "MAX_PAGES", "PROCESSING_TIMEOUT_MINUTES",
        "CHUNK_SIZE", "CHUNK_OVERLAP",
        *R2_VARIABLES,
    ]:
        monkeypatch.delenv(name, raising=False)


def make(**changes):
    values = {**VALID, **changes}
    return Settings(_env_file=None, **{k: v for k, v in values.items() if v is not None})


@pytest.mark.parametrize(
    "given, expected",
    [
        # Neon style: plain postgresql:// with SSL options
        (
            "postgresql://user:pw@ep-example.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+psycopg://user:pw@ep-example.neon.tech/neondb?sslmode=require&channel_binding=require",
        ),
        # Older postgres:// style
        ("postgres://user:pw@host:5432/db", "postgresql+psycopg://user:pw@host:5432/db"),
        # Already correct: left alone
        ("postgresql+psycopg://user:pw@db:5432/db", "postgresql+psycopg://user:pw@db:5432/db"),
    ],
)
def test_database_url_uses_psycopg_driver(given, expected):
    assert make(database_url=given).database_url == expected


@pytest.mark.parametrize("missing", ["DATABASE_URL", "JWT_SECRET", *R2_VARIABLES])
def test_missing_required_setting_refuses_to_start(missing):
    with pytest.raises(ValidationError) as error:
        make(**{missing.lower(): None})

    assert missing.lower() in str(error.value)  # the error names the missing setting


@pytest.mark.parametrize("variable", R2_VARIABLES)
def test_empty_r2_setting_refuses_to_start(variable):
    with pytest.raises(ValidationError) as error:
        make(**{variable.lower(): "   "})

    assert f"{variable} is empty" in str(error.value)


def test_settings_errors_never_show_secret_values():
    # Short on purpose: pydantic shortens long values in errors, which could
    # hide a leak in this test while still printing part of a real secret
    secret = "s3cr3t"

    with pytest.raises(ValidationError) as error:
        make(database_url=None, jwt_secret=secret, r2_secret_access_key=secret, r2_account_id="bad/id")

    message = str(error.value)
    assert "database_url" in message and "r2_account_id" in message
    assert secret not in message
    assert "bad/id" not in message
    assert "input_value" not in message  # no input values printed at all


def test_r2_secret_is_hidden_when_settings_are_printed():
    settings = make(r2_secret_access_key="very-secret-value")

    assert "very-secret-value" not in repr(settings)
    assert "very-secret-value" not in str(settings)
    assert settings.r2_secret_access_key.get_secret_value() == "very-secret-value"


@pytest.mark.parametrize("bad", ["evil.com/x", "a.b", "user@host", "id with space"])
def test_r2_account_id_cannot_change_the_endpoint_host(bad):
    with pytest.raises(ValidationError, match="R2_ACCOUNT_ID may only contain"):
        make(r2_account_id=bad)


def test_signed_url_seconds_defaults_to_300_and_must_be_positive():
    assert make().signed_url_seconds == 300
    assert make(signed_url_seconds=60).signed_url_seconds == 60
    for bad in (0, -1, 8 * 24 * 3600):
        with pytest.raises(ValidationError):
            make(signed_url_seconds=bad)


def test_empty_database_url_is_rejected_clearly():
    with pytest.raises(ValidationError) as error:
        make(database_url="  ")

    assert "DATABASE_URL is empty" in str(error.value)


def test_empty_jwt_secret_is_rejected():
    with pytest.raises(ValidationError) as error:
        make(jwt_secret=" ")

    assert "JWT_SECRET is empty" in str(error.value)


def test_jwt_expire_minutes_defaults_to_30():
    assert make().jwt_expire_minutes == 30


def test_upload_limits_default_to_20_mb_and_10_documents():
    settings = make()

    assert settings.max_upload_mb == 20
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.max_documents_per_workspace == 10


def test_upload_limits_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_MB", "5")
    monkeypatch.setenv("MAX_DOCUMENTS_PER_WORKSPACE", "3")

    settings = make()

    assert settings.max_upload_bytes == 5 * 1024 * 1024
    assert settings.max_documents_per_workspace == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"max_upload_mb": 0},
        {"max_upload_mb": -1},
        {"max_upload_mb": 101},  # uploads are held in memory while checked
        {"max_documents_per_workspace": 0},
    ],
)
def test_impossible_upload_limits_refuse_to_start(changes):
    with pytest.raises(ValidationError):
        make(**changes)


def test_processing_limits_default_to_100_pages_and_15_minutes():
    settings = make()

    assert settings.max_pages == 100
    assert settings.processing_timeout_minutes == 15


@pytest.mark.parametrize("changes", [{"max_pages": 0}, {"processing_timeout_minutes": 0}])
def test_impossible_processing_limits_refuse_to_start(changes):
    with pytest.raises(ValidationError):
        make(**changes)


def test_chunking_defaults_to_1000_characters_with_150_overlap():
    settings = make()

    assert (settings.chunk_size, settings.chunk_overlap) == (1000, 150)


@pytest.mark.parametrize(
    "changes",
    [{"chunk_overlap": 1000}, {"chunk_size": 500, "chunk_overlap": 600}, {"chunk_size": 100}, {"chunk_overlap": -1}],
)
def test_impossible_chunking_settings_refuse_to_start(changes):
    with pytest.raises(ValidationError):
        make(**changes)
