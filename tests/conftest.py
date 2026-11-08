"""Shared test fixtures.

Tests use their own database (the development database name + "_test"),
which is emptied before every test.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.dependencies import get_db, get_session_factory, get_storage
from app.main import app
from app.services import storage as storage_module
from app.services.storage import FakeStorage

DEV_DATABASE_URL = make_url(settings.database_url)
TEST_DATABASE_URL = DEV_DATABASE_URL.set(database=f"{DEV_DATABASE_URL.database}_test")
assert TEST_DATABASE_URL.database != DEV_DATABASE_URL.database


def create_database_if_missing(name: str) -> None:
    # Connect to PostgreSQL's built-in "postgres" database to create ours
    admin_engine = create_engine(
        DEV_DATABASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin_engine.dispose()


@pytest.fixture(scope="session")
def engine():
    """Create the test database and its tables once per test run."""
    create_database_if_missing(TEST_DATABASE_URL.database)
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    """Empty every table before each test, so each test starts clean."""
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    """A database session for tests to check what was saved."""
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def client(session_factory):
    """A TestClient whose requests use the test database."""

    def get_test_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = get_test_db
    # Background tasks and the startup check make their own sessions
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def fake_storage():
    """Used automatically by every test: the app stores files in memory.

    Replaced through FastAPI's dependency overrides, the same way as the
    database, so the app itself has no test mode switch.
    """
    fake = FakeStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture(autouse=True)
def no_network_for_storage(monkeypatch):
    """Safety net: if any test reaches botocore's HTTP layer (a call to R2
    that wasn't stubbed), fail loudly instead of sending it."""

    def network_call(*args, **kwargs):
        raise RuntimeError("A test tried to reach the network through botocore")

    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", network_call)
    storage_module.default_storage.cache_clear()
    yield
    storage_module.default_storage.cache_clear()


@pytest.fixture
def register(client):
    """register(email, password) -> the response from POST /auth/register."""

    def _register(email="alice@example.com", password="correct-horse"):
        return client.post("/auth/register", json={"email": email, "password": password})

    return _register


@pytest.fixture
def auth_headers(client, register):
    """auth_headers(email, password) -> registers and logs in, returns the Bearer header."""

    def _auth_headers(email="alice@example.com", password="correct-horse"):
        assert register(email, password).status_code == 201
        response = client.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _auth_headers


@pytest.fixture
def make_document(db, fake_storage):
    """make_document(email, **changes) -> a Document saved straight to the
    database (and its file to FakeStorage), in the user's personal workspace.
    The user must already be registered."""
    import hashlib
    import uuid

    from sqlalchemy import select

    from app.models import Document, User
    from app.models.document import STATUS_PROCESSING
    from app.services.storage import document_key
    from app.services.workspaces import get_personal_workspace

    def _make_document(email="alice@example.com", data: bytes | None = None, **changes):
        user = db.scalars(select(User).where(User.email == email)).one()
        workspace = get_personal_workspace(db, user.id)
        document_id = uuid.uuid4()
        data = data if data is not None else b"%PDF-1.7 " + document_id.bytes
        key = document_key(workspace.id, document_id)
        values = dict(
            id=document_id,
            workspace_id=workspace.id,
            uploaded_by=user.id,
            title="Handbook",
            original_filename="Handbook.pdf",
            storage_key=key,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            status=STATUS_PROCESSING,
        )
        document = Document(**{**values, **changes})
        db.add(document)
        db.commit()
        fake_storage.save(key, data, "application/pdf")
        return document

    return _make_document
