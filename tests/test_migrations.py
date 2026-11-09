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
DOCUMENTS_REVISION = "7c2e9a41d5b3"
PAGES_REVISION = "a3f1c8e6b902"
CHUNKS_REVISION = "c5d2e7f84a16"
CONVERSATIONS_REVISION = "e8b3f0a2c7d4"
M1_TABLES = {"users", "workspaces", "workspace_members", "documents", "document_pages"}
RUN_A_TABLES = M1_TABLES | {"document_sections", "document_chunks"}
CONVERSATION_TABLES = RUN_A_TABLES | {"conversations", "messages"}
ALL_TABLES = CONVERSATION_TABLES | {"question_usage"}
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

    command.downgrade(alembic_config, "-1")  # undo only the question usage migration
    assert tables(migrations_engine) == CONVERSATION_TABLES

    command.downgrade(alembic_config, "-1")  # then only the conversations migration
    assert tables(migrations_engine) == RUN_A_TABLES

    command.downgrade(alembic_config, "-1")  # then only the sections and chunks migration
    assert tables(migrations_engine) == M1_TABLES

    command.downgrade(alembic_config, "-1")  # then only the pages migration
    assert tables(migrations_engine) == {"users", "workspaces", "workspace_members", "documents"}

    command.downgrade(alembic_config, "-1")  # then only the documents migration
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


def test_downgrading_pages_keeps_documents(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")
    with migrations_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash) VALUES (gen_random_uuid(), 'alice@example.com', 'x')"
        ))
        workspace_id = conn.scalar(text(
            "INSERT INTO workspaces (id, name, kind) VALUES (gen_random_uuid(), 'Personal', 'personal') RETURNING id"
        ))
        conn.execute(
            text(
                "INSERT INTO documents (id, workspace_id, title, original_filename, storage_key, "
                "size_bytes, sha256, status) VALUES (gen_random_uuid(), :w, 't', 't.pdf', 'k', 1, "
                "repeat('a', 64), 'processing')"
            ),
            {"w": workspace_id},
        )

    command.downgrade(alembic_config, DOCUMENTS_REVISION)

    assert "document_pages" not in tables(migrations_engine)
    with migrations_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM documents")) == 1


def test_chunks_have_a_halfvec_embedding_with_an_hnsw_cosine_index(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    with migrations_engine.connect() as conn:
        column_type = conn.scalar(text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        ))
        index = conn.scalar(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_document_chunks_embedding_hnsw'"
        ))
    assert column_type == "halfvec(2048)"
    assert "USING hnsw" in index and "halfvec_cosine_ops" in index
    assert "ix_document_chunks_document_id" in {i["name"] for i in inspect(migrations_engine).get_indexes("document_chunks")}


def test_downgrading_sections_and_chunks_keeps_pages(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, PAGES_REVISION)

    assert tables(migrations_engine) == M1_TABLES


def test_downgrading_conversations_keeps_chunks(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, CHUNKS_REVISION)

    assert tables(migrations_engine) == RUN_A_TABLES


def test_downgrading_question_usage_keeps_conversations(migrations_engine, alembic_config):
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, CONVERSATIONS_REVISION)

    assert tables(migrations_engine) == CONVERSATION_TABLES
