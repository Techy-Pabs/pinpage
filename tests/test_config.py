import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
    settings = Settings(_env_file=None, database_url=given, jwt_secret="test")

    assert settings.database_url == expected


@pytest.mark.parametrize("missing", ["DATABASE_URL", "JWT_SECRET"])
def test_missing_required_setting_refuses_to_start(monkeypatch, missing):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    values = {"database_url": "postgresql://u:p@h/db", "jwt_secret": "test"}
    values.pop(missing.lower())

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)

    assert missing.lower() in str(error.value)  # the error names the missing setting


def test_settings_error_does_not_show_secret_values(monkeypatch):
    # Simulate a deployment where DATABASE_URL is missing
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Short on purpose: pydantic shortens long values in errors, which could
    # hide a leak in this test while still printing part of a real secret
    secret = "s3cr3t"

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, jwt_secret=secret)

    message = str(error.value)
    assert "database_url" in message
    assert secret not in message
    assert "input_value" not in message  # no input values printed at all


def test_empty_database_url_is_rejected_clearly():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="  ", jwt_secret="test")

    assert "DATABASE_URL is empty" in str(error.value)


def test_empty_jwt_secret_is_rejected():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="postgresql://u:p@h/db", jwt_secret=" ")

    assert "JWT_SECRET is empty" in str(error.value)


def test_jwt_expire_minutes_defaults_to_30(monkeypatch):
    monkeypatch.delenv("JWT_EXPIRE_MINUTES", raising=False)

    settings = Settings(_env_file=None, database_url="postgresql://u:p@h/db", jwt_secret="test")

    assert settings.jwt_expire_minutes == 30
