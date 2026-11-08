"""The Alembic migrations, run for real against their own empty database.

The other tests build tables straight from the models, so without this
test a broken migration would only show up when the app starts.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.core.config import settings
from app.db.base import Base
from tests.conftest import DEV_DATABASE_URL, create_database_if_missing

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
USERS_REVISION = "bf4bd5376b01"
WORKSPACES_REVISION = "0da6b09ad8ef"
ALL_TABLES = {"users", "workspaces", "workspace_members", "documents"}
MIGRATIONS_DATABASE = f"{DEV_DATABASE_URL.database}_migrations_test"
assert MIGRATIONS_DATABASE != DEV_DATABASE_URL.database


def drop_database(name: str) -> None:
    admin_engine = create_engine(DEV_DATABASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin_engine.dispose()


@pytest.fixture
def migrations_engine(monkeypatch):
    """A brand new empty database, with Alembic pointed at it."""
    drop_database(MIGRATIONS_DATABASE)
    create_database_if_missing(MIGRATIONS_DATABASE)
    url = DEV_DATABASE_URL.set(database=MIGRATIONS_DATABASE)
    # alembic/env.py reads the URL from settings, so point it here
    monkeypatch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
    engine = create_engine(url)
    yield engine
    engine.dispose()
    drop_database(MIGRATIONS_DATABASE)


@pytest.fixture
def alembic_config():
    return Config(str(ALEMBIC_INI))


def tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_matches_the_models(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    assert tables(migrations_engine) == ALL_TABLES
    with migrations_engine.connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert differences == []  # the migrations build exactly what the models describe


def test_upgrade_downgrade_and_upgrade_again(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, "-1")  # undo only the documents migration
    assert tables(migrations_engine) == {"users", "workspaces", "workspace_members"}

    command.downgrade(alembic_config, "-1")  # then only the workspaces migration
    assert tables(migrations_engine) == {"users"}

    command.downgrade(alembic_config, "base")
    assert tables(migrations_engine) == set()

    command.upgrade(alembic_config, "head")
    assert tables(migrations_engine) == ALL_TABLES


def test_downgrading_workspaces_keeps_existing_users(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")
    with migrations_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (gen_random_uuid(), 'alice@example.com', 'x')"
        ))

    command.downgrade(alembic_config, USERS_REVISION)

    with migrations_engine.connect() as conn:
        assert conn.scalar(text("SELECT email FROM users")) == "alice@example.com"


def test_downgrading_documents_keeps_users_and_workspaces(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")
    with migrations_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (gen_random_uuid(), 'alice@example.com', 'x')"
        ))
        conn.execute(text("INSERT INTO workspaces (id, name, kind) VALUES (gen_random_uuid(), 'Personal', 'personal')"))

    command.downgrade(alembic_config, WORKSPACES_REVISION)

    assert tables(migrations_engine) == {"users", "workspaces", "workspace_members"}
    with migrations_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users")) == 1
        assert conn.scalar(text("SELECT count(*) FROM workspaces")) == 1
