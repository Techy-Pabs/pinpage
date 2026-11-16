"""What the frontend needs from the backend (Milestone 5, slice 0): CORS,
GET /usage, and the local fake AI. FakeAI and FakeStorage only; nothing
here contacts OpenRouter or R2."""

import os
import subprocess
import sys
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.dependencies import get_ai
from app.main import app
from app.models import Document, DocumentChunk, Message, QuestionUsage, User
from app.models.document import STATUS_READY
from app.services import ai as ai_module
from app.services import answering
from app.services.ai import AIServiceError, FakeAI, LocalFakeAI, OpenRouterAI
from app.services.prompts import NOT_FOUND_MESSAGE
from tests.pdfs import LONG_TEXT, outlined_pdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALID = dict(
    database_url="postgresql://u:p@h/db", jwt_secret="x", r2_account_id="a", r2_access_key_id="b",
    r2_secret_access_key="c", r2_bucket="d",
)


def make_settings(**changes) -> Settings:
    return Settings(_env_file=None, **{**VALID, **changes})


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


# --- CORS ------------------------------------------------------------------------------------------

ALLOWED = settings.allowed_origin_list[0]
OTHER = "https://evil.example.com"


def preflight(client, origin, path="/auth/login", method="POST"):
    return client.options(path, headers={
        "Origin": origin, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization, content-type",
    })


def test_cors_allows_a_listed_origin(client):
    response = preflight(client, ALLOWED)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers  # the token is a header, not a cookie

    answer = client.get("/health", headers={"Origin": ALLOWED})
    assert answer.headers["access-control-allow-origin"] == ALLOWED
    assert answer.headers["access-control-expose-headers"] == "Retry-After"  # read on a 429


def test_cors_refuses_any_other_origin(client):
    response = preflight(client, OTHER)

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": OTHER}).headers
    # A look-alike of an allowed origin is another origin
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": ALLOWED + ".evil.com"}).headers


def test_allowed_origins_default_to_the_local_frontend():
    assert Settings.model_fields["allowed_origins"].default == "http://localhost:5173"
    assert make_settings().allowed_origin_list == ["http://localhost:5173"]


def test_allowed_origins_are_a_comma_separated_list_of_exact_origins():
    configured = make_settings(allowed_origins=" http://localhost:5173/ , https://pinpage.example.com ")

    assert configured.allowed_origin_list == ["http://localhost:5173", "https://pinpage.example.com"]


@pytest.mark.parametrize("origins", ["*", "https://*.example.com", "localhost:5173", "https://example.com/app", "ftp://x.com"])
def test_wildcards_and_paths_are_refused(origins):
    with pytest.raises(ValidationError, match="ALLOWED_ORIGINS"):
        make_settings(allowed_origins=origins)


# --- Usage -------------------------------------------------------------------------------------------

def test_usage_needs_a_login(client):
    assert client.get("/usage").status_code == 401


def test_usage_starts_at_zero_and_resets_at_the_next_midnight_utc(client, alice, monkeypatch):
    monkeypatch.setattr(answering, "today_utc", lambda: date(2026, 11, 16))

    assert client.get("/usage", headers=alice).json() == {
        "questions_used_today": 0,
        "questions_per_day": settings.daily_questions_per_user,
        "resets_at": "2026-11-17T00:00:00Z",
    }


def test_usage_counts_today_only_and_resets_at_midnight_utc(client, db, alice, auth_headers, monkeypatch):
    bob = auth_headers("bob@example.com")
    alice_id = db.scalar(select(User.id).where(User.email == "alice@example.com"))
    bob_id = db.scalar(select(User.id).where(User.email == "bob@example.com"))
    db.add_all([
        QuestionUsage(user_id=alice_id, used_on=date(2026, 11, 15), count=19),
        QuestionUsage(user_id=alice_id, used_on=date(2026, 11, 16), count=3),
        QuestionUsage(user_id=bob_id, used_on=date(2026, 11, 16), count=7),
    ])
    db.commit()

    monkeypatch.setattr(answering, "today_utc", lambda: date(2026, 11, 16))
    assert client.get("/usage", headers=alice).json()["questions_used_today"] == 3  # not yesterday's, not Bob's
    assert client.get("/usage", headers=bob).json()["questions_used_today"] == 7

    # After midnight UTC
    monkeypatch.setattr(answering, "today_utc", lambda: date(2026, 11, 17))
    body = client.get("/usage", headers=alice).json()
    assert body["questions_used_today"] == 0 and body["resets_at"] == "2026-11-18T00:00:00Z"


# --- The local fake AI ---------------------------------------------------------------------------------

def test_app_env_defaults_to_production_and_the_ai_to_openrouter():
    configured = make_settings()

    assert (configured.app_env, configured.ai_provider) == ("production", "openrouter")


@pytest.mark.parametrize("app_env", [None, "production"])
def test_the_fake_ai_is_refused_outside_development(app_env):
    changes = {"ai_provider": "fake", **({"app_env": app_env} if app_env else {})}

    with pytest.raises(ValidationError, match="AI_PROVIDER=fake is only allowed with APP_ENV=development"):
        make_settings(**changes)


def test_the_fake_ai_is_allowed_in_development():
    assert make_settings(ai_provider="fake", app_env="development").ai_provider == "fake"


@pytest.mark.parametrize(("app_env", "starts"), [("production", False), ("development", True)])
def test_the_app_refuses_to_start_with_the_fake_ai_outside_development(app_env, starts):
    # A fresh process, as when the server starts. Importing the app contacts nothing.
    env = {**os.environ, "AI_PROVIDER": "fake", "APP_ENV": app_env}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert (result.returncode == 0) is starts
    if not starts:
        assert "AI_PROVIDER=fake is only allowed with APP_ENV=development" in result.stderr


def test_the_fake_provider_gives_the_local_fake_ai(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "ai_provider", "fake")
    ai_module.default_ai.cache_clear()
    try:
        ai = ai_module.default_ai()
        assert isinstance(ai, LocalFakeAI) and ai.is_fake
        assert ai is ai_module.default_ai()
    finally:
        ai_module.default_ai.cache_clear()
    monkeypatch.setattr(settings, "ai_provider", "openrouter")
    try:
        assert isinstance(ai_module.default_ai(), OpenRouterAI)
    finally:
        ai_module.default_ai.cache_clear()


def test_the_local_fake_ai_answers_citing_the_first_excerpt_and_records_nothing():
    fake = LocalFakeAI(dimensions=8)

    reply = fake.generate('Rules ... <excerpts document="Handbook">...</excerpts>', [{"role": "user", "content": "Q?"}])

    assert reply.text.endswith("[S1].") and not reply.truncated
    assert fake.embed_batch(["annual leave"], "query") == [fake.vector_for("annual leave")]
    assert fake.reply_calls == [] and fake.embed_calls == []
    with pytest.raises(AIServiceError):
        fake.generate("Rewrite the follow up question.", [])  # no excerpts: a rewrite


class RealAI(FakeAI):
    """Stands in for OpenRouterAI: fixed vectors, but not marked fake."""

    is_fake = False


@pytest.fixture
def use_ai():
    """use_ai(ai) makes the app use ai from now on in this test."""

    def _use(ai):
        app.dependency_overrides[get_ai] = lambda: ai
        return ai

    return _use


@pytest.fixture
def handbook(client, alice):
    """upload() -> the id of a small handbook uploaded and processed by whichever AI the app uses."""
    data = outlined_pdf(
        [f"1 Leave\nStaff get 20 days of annual leave. {LONG_TEXT}", f"2 Pay\nSalaries are paid monthly. {LONG_TEXT}"],
        [("1 Leave", 1, 1), ("2 Pay", 1, 2)],
    )

    def upload():
        response = client.post("/documents", headers=alice, files={"file": ("handbook.pdf", data, "application/pdf")})
        assert response.status_code == 202
        return response.json()["id"]

    return upload


def ask(client, headers, document_id, question="How many days of annual leave do staff get?"):
    conversation = client.post(f"/documents/{document_id}/conversations", headers=headers).json()["id"]
    return client.post(f"/conversations/{conversation}/messages", headers=headers, json={"content": question})


def test_with_the_fake_ai_the_whole_flow_works_and_answers_cite_an_excerpt(client, db, alice, handbook, use_ai, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_min_similarity", -1.0)
    use_ai(LocalFakeAI(settings.embedding_dimensions))

    document_id = handbook()

    document = db.get(Document, document_id)
    assert document.status == STATUS_READY and document.embedded_with_fake_ai is True
    answer = ask(client, alice, document_id).json()
    assert answer["outcome"] == "answered" and answer["content"].endswith("[S1].")
    assert [c["marker"] for c in answer["citations"]] == ["S1"]
    page = ask(client, alice, document_id, "What does page 2 say?").json()
    assert page["mode"] == "page" and page["citations"][0]["page_start"] == 2
    summary = ask(client, alice, document_id, "Summarise section 1").json()
    assert summary["mode"] == "section" and summary["outcome"] == "answered"


def test_a_document_embedded_with_the_real_ai_is_not_marked(db, handbook, use_ai):
    use_ai(RealAI())

    document_id = handbook()

    assert db.get(Document, document_id).embedded_with_fake_ai is False


def test_a_fake_embedded_document_cannot_be_asked_with_the_real_ai_until_it_is_retried(
    client, db, alice, handbook, use_ai, monkeypatch
):
    monkeypatch.setattr(settings, "retrieval_min_similarity", -1.0)
    use_ai(LocalFakeAI(settings.embedding_dimensions))
    document_id = handbook()
    chunks = count(db, DocumentChunk)

    real = use_ai(RealAI())
    real.reply = "Staff get 20 days [S1]."
    refused = ask(client, alice, document_id)

    assert refused.status_code == 409
    assert refused.json()["error"] == {
        "code": "embedded_with_fake_ai",
        "message": "This document was prepared with the local test AI. Retry it to prepare it for real answers.",
    }
    assert real.embed_calls == [] and real.reply_calls == []  # no AI request
    assert count(db, Message) == 0 and count(db, QuestionUsage) == 0  # nothing saved or counted

    # Retrying with the real AI embeds every chunk again and clears the mark
    retried = client.post(f"/documents/{document_id}/retry", headers=alice)
    assert retried.status_code == 202
    db.expire_all()
    document = db.get(Document, document_id)
    assert document.status == STATUS_READY and document.embedded_with_fake_ai is False
    assert sum(len(c["texts"]) for c in real.embed_calls if c["mode"] == "document") == chunks
    assert db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.embedding.is_(None))) == 0

    answered = ask(client, alice, document_id)
    assert answered.status_code == 201 and answered.json()["content"] == "Staff get 20 days [S1]."


def test_retrying_a_ready_document_is_still_refused_otherwise(client, alice, handbook, use_ai):
    use_ai(RealAI())
    real_document = handbook()
    assert client.post(f"/documents/{real_document}/retry", headers=alice).json()["error"]["code"] == "retry_not_allowed"

    # Still on the fake AI, a fake embedded document has nothing to redo
    use_ai(LocalFakeAI(settings.embedding_dimensions))
    client.delete(f"/documents/{real_document}", headers=alice)
    fake_document = handbook()
    assert client.post(f"/documents/{fake_document}/retry", headers=alice).json()["error"]["code"] == "retry_not_allowed"


def test_the_fake_ai_may_ask_about_any_document(client, alice, handbook, use_ai, monkeypatch):
    # Only the real AI is protected from fake vectors; locally anything goes
    monkeypatch.setattr(settings, "retrieval_min_similarity", -1.0)
    use_ai(RealAI())
    document_id = handbook()

    use_ai(LocalFakeAI(settings.embedding_dimensions))
    response = ask(client, alice, document_id)

    assert response.status_code == 201 and response.json()["content"] != NOT_FOUND_MESSAGE
