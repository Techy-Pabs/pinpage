"""Front matter end to end, and the rebuild-sections command. FakeAI and
FakeStorage only."""

import uuid

import pytest
from sqlalchemy import select, update

from app.cli import documents as documents_cli
from app.core.config import settings
from app.models import DocumentChunk, DocumentSection
from tests.pdfs import LONG_TEXT, outlined_pdf

# A cover and a contents page, then sections from bookmarks. Every page has
# a running header, as in the Lantern Hill handbook.
HEADER = "LANTERN HILL STAFF HANDBOOK"
PAGES = [
    f"{HEADER}\nStaff Handbook\nEdition 4",
    f"{HEADER}\nContents\n1 Introduction ..... 3\n2 Leave ..... 4",
    f"{HEADER}\n1 Introduction\nWelcome to the college. {LONG_TEXT}",
    f"{HEADER}\nMore about the college. {LONG_TEXT}",
    f"{HEADER}\n2 Leave\nAnnual leave is 25 days. {LONG_TEXT}",
]
OUTLINE = [("1 Introduction", 1, 3), ("2 Leave", 1, 5)]


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    monkeypatch.setattr(settings, "chunk_size", 1000)
    monkeypatch.setattr(settings, "chunk_overlap", 150)


@pytest.fixture
def alice(auth_headers):
    return auth_headers("alice@example.com")


@pytest.fixture
def handbook(client, alice):
    """The id of the handbook above, processed and ready."""
    response = client.post(
        "/documents", headers=alice,
        files={"file": ("Handbook.pdf", outlined_pdf(PAGES, OUTLINE), "application/pdf")},
    )
    assert response.status_code == 202
    return uuid.UUID(response.json()["id"])


def sections_of(db, document_id):
    db.expire_all()
    return db.scalars(
        select(DocumentSection).where(DocumentSection.document_id == document_id).order_by(DocumentSection.position)
    ).all()


def chunk_state(db, document_id):
    """Each chunk's id, text, pages and embedding, in order."""
    db.expire_all()
    rows = db.execute(
        select(DocumentChunk.id, DocumentChunk.content, DocumentChunk.page_start, DocumentChunk.page_end,
               DocumentChunk.embedding)
        .where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.position)
    ).all()
    return [(r[0], r[1], r[2], r[3], r[4].to_list()) for r in rows]


def make_it_look_old(db, document_id):
    """As processed before Milestone 3: no front matter (the pages before the
    first bookmark were a section named after the document) and section 1
    ending on the next section's start page."""
    sections = sections_of(db, document_id)
    db.execute(update(DocumentSection).where(DocumentSection.id == sections[0].id)
               .values(title="Handbook", kind="body"))
    db.execute(update(DocumentSection).where(DocumentSection.id == sections[1].id).values(end_page=5))
    db.commit()


# --- Front matter, end to end ------------------------------------------------------------------

def test_new_uploads_get_front_matter_and_fixed_ends(client, db, alice, handbook):
    response = client.get(f"/documents/{handbook}/sections", headers=alice)

    assert [(s["title"], s["kind"], s["start_page"], s["end_page"]) for s in response.json()] == [
        ("Front matter", "front_matter", 1, 2),
        ("Introduction", "body", 3, 4),  # not 3 to 5: section 2 starts at the top of page 5
        ("Leave", "body", 5, 5),
    ]


def test_front_matter_chunks_are_never_retrieved(client, db, alice, handbook, fake_ai):
    # The question matches the contents page exactly, and the body not at all
    front_matter = sections_of(db, handbook)[0]
    contents_chunk = db.scalars(select(DocumentChunk).where(DocumentChunk.section_id == front_matter.id)).first()
    question = "Where is the contents page?"
    fake_ai.vectors[question] = contents_chunk.embedding.to_list()
    fake_ai.reply = "On page 2 [S1]."
    conversation = client.post(f"/documents/{handbook}/conversations", headers=alice).json()["id"]

    body = client.post(f"/conversations/{conversation}/messages", headers=alice, json={"content": question}).json()

    for call in fake_ai.reply_calls:
        assert "Contents" not in call["system_prompt"]
    assert all(c["section_title"] != "Front matter" for c in body["citations"])


# --- The rebuild command ------------------------------------------------------------------------

def test_rebuild_fixes_old_sections_without_re_embedding(db, handbook, session_factory, fake_storage, fake_ai):
    make_it_look_old(db, handbook)
    before = chunk_state(db, handbook)
    old_ids = {s.id for s in sections_of(db, handbook)}
    embed_calls = len(fake_ai.embed_calls)

    result = documents_cli.rebuild_sections(session_factory, fake_storage, handbook)

    sections = sections_of(db, handbook)
    assert [(s.title, s.kind, s.start_page, s.end_page) for s in sections] == [
        ("Front matter", "front_matter", 1, 2), ("Introduction", "body", 3, 4), ("Leave", "body", 5, 5),
    ]
    assert not old_ids & {s.id for s in sections}  # new rows; the old ones are gone
    assert chunk_state(db, handbook) == before  # same ids, text, pages and embeddings
    assert len(fake_ai.embed_calls) == embed_calls  # nothing was embedded
    # Every chunk now points at one of the new sections
    db.expire_all()
    assert set(db.scalars(select(DocumentChunk.section_id).where(DocumentChunk.document_id == handbook))) <= {
        s.id for s in sections
    }
    assert (result.old_sections, result.new_sections, result.front_matter, result.chunks_moved) == (3, 3, 1, len(before))


def test_rebuild_refuses_when_chunk_boundaries_would_change(db, handbook, session_factory, fake_storage, monkeypatch):
    make_it_look_old(db, handbook)
    before_sections = [(s.id, s.title, s.end_page) for s in sections_of(db, handbook)]
    before_chunks = chunk_state(db, handbook)
    monkeypatch.setattr(settings, "chunk_size", 100)  # the same text would now be cut differently
    monkeypatch.setattr(settings, "chunk_overlap", 10)

    with pytest.raises(documents_cli.RebuildRefused, match="Upload the document again"):
        documents_cli.rebuild_sections(session_factory, fake_storage, handbook)

    assert [(s.id, s.title, s.end_page) for s in sections_of(db, handbook)] == before_sections
    assert chunk_state(db, handbook) == before_chunks


def test_rebuild_refuses_a_missing_document(session_factory, fake_storage):
    with pytest.raises(documents_cli.RebuildRefused, match="No document"):
        documents_cli.rebuild_sections(session_factory, fake_storage, uuid.uuid4())


def test_rebuild_refuses_a_document_without_chunks(register, make_document, session_factory, fake_storage):
    register()
    document = make_document()

    with pytest.raises(documents_cli.RebuildRefused, match="no stored pages or chunks"):
        documents_cli.rebuild_sections(session_factory, fake_storage, document.id)


def test_rebuild_refuses_when_storage_is_down(handbook, session_factory, fake_storage):
    fake_storage.fail_on = {"read"}

    with pytest.raises(documents_cli.RebuildRefused, match="bookmarks"):
        documents_cli.rebuild_sections(session_factory, fake_storage, handbook)


# --- The command line ----------------------------------------------------------------------------

@pytest.fixture
def command(monkeypatch, session_factory, fake_storage):
    monkeypatch.setattr(documents_cli, "SessionLocal", session_factory)
    monkeypatch.setattr(documents_cli, "default_storage", lambda: fake_storage)

    def _run(*args, answer=None):
        if answer is not None:
            monkeypatch.setattr("builtins.input", lambda prompt: answer)
        return documents_cli.main(["rebuild-sections", *args])

    return _run


def test_command_rebuilds_and_prints_counts_only(command, capsys, db, handbook):
    make_it_look_old(db, handbook)

    assert command(str(handbook), "--yes") == 0

    out = capsys.readouterr().out
    assert "Database:" in out
    assert "3 sections (was 3), 1 front matter" in out
    assert "No text or embedding changed." in out
    assert "Welcome" not in out and "Leave" not in out  # no document text or titles


def test_command_asks_first(command, capsys, db, handbook):
    make_it_look_old(db, handbook)

    assert command(str(handbook), answer="n") == 1

    assert "Cancelled. Nothing was changed." in capsys.readouterr().out
    assert sections_of(db, handbook)[0].title == "Handbook"


def test_command_explains_a_refusal(command, capsys):
    assert command(str(uuid.uuid4()), "--yes") == 1
    assert "No document with that id. Nothing was changed." in capsys.readouterr().out


def test_command_rejects_a_malformed_id(command, capsys):
    assert command("not-an-id", "--yes") == 1
    assert "not a document id" in capsys.readouterr().out
