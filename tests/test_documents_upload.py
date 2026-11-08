"""POST /documents. Files go to FakeStorage only; nothing contacts R2."""

import asyncio
import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.main import app
from app.models import Document, Workspace, WorkspaceMember
from app.models.document import STATUS_FAILED, STATUS_PROCESSING
from app.routers import documents as documents_router
from app.services import documents as docs
from app.services import uploads
from app.services.storage import document_key
from tests.pdfs import make_pdf

MB = 1024 * 1024
RESPONSE_FIELDS = {
    "id", "title", "original_filename", "size_bytes", "page_count", "status",
    "error_code", "error_message", "created_at", "processed_at",
}


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    # Fixed here, so the tests don't depend on .env. 1 MB keeps the
    # too-large files small.
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    monkeypatch.setattr(settings, "max_documents_per_workspace", 10)


@pytest.fixture(autouse=True)
def no_processing(monkeypatch):
    # These tests are about the upload itself; processing has its own tests.
    # Without this, the small test PDFs would be processed (and fail as
    # scans) right after each upload, changing what counts toward the limit.
    monkeypatch.setattr(documents_router, "process_document", lambda *args: None)


@pytest.fixture
def upload(client):
    """upload(headers, data, filename, title=None, content_type) -> the response."""

    def _upload(headers, data=None, filename="handbook.pdf", title=None, content_type="application/pdf"):
        data = make_pdf() if data is None else data
        form = {"title": title} if title is not None else None
        return client.post("/documents", headers=headers, files={"file": (filename, data, content_type)}, data=form)

    return _upload


@pytest.fixture
def server_error_client(client):
    """A crash comes back as the 500 a real caller would get."""
    return TestClient(app, raise_server_exceptions=False)


def count_documents(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Document))


def workspace_of(db, owner_id: uuid.UUID) -> Workspace:
    return db.scalars(
        select(Workspace).join(WorkspaceMember).where(WorkspaceMember.user_id == owner_id)
    ).one()


def user_id(client, headers) -> uuid.UUID:
    return uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])


def multipart_body(data: bytes, filename: str = "big.pdf", boundary: str = "pinpage-test-boundary") -> tuple[bytes, str]:
    # Built by hand: httpx would tidy up a strange file name before sending,
    # and a hostile client wouldn't
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


# --- A good upload ------------------------------------------------------------------

def test_good_upload_returns_202_processing_and_stores_the_file(client, db, upload, auth_headers, fake_storage):
    headers = auth_headers()
    pdf = make_pdf("Staff handbook")

    response = upload(headers, pdf, filename="Staff Handbook.pdf")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["status"] == STATUS_PROCESSING
    assert body["title"] == "Staff Handbook"
    assert body["original_filename"] == "Staff Handbook.pdf"
    assert body["size_bytes"] == len(pdf)
    assert body["page_count"] is None and body["processed_at"] is None
    assert body["error_code"] is None and body["error_message"] is None

    workspace = workspace_of(db, user_id(client, headers))
    key = document_key(workspace.id, uuid.UUID(body["id"]))
    assert fake_storage.files == {key: pdf}  # stored under the UUID key, nothing else
    assert fake_storage.content_types[key] == "application/pdf"

    document = db.get(Document, uuid.UUID(body["id"]))
    assert document.workspace_id == workspace.id
    assert document.uploaded_by == user_id(client, headers)
    assert document.storage_key == key
    assert document.sha256 == hashlib.sha256(pdf).hexdigest()


def test_upload_schedules_processing_for_the_new_document(upload, auth_headers, monkeypatch, fake_storage):
    scheduled = []
    monkeypatch.setattr(documents_router, "process_document", lambda *args: scheduled.append(args))

    response = upload(auth_headers())

    [(document_id, storage, session_factory)] = scheduled
    assert document_id == uuid.UUID(response.json()["id"])
    assert storage is fake_storage  # the same storage the request used
    assert session_factory is not None  # its own sessions, not the request's


def test_title_given_by_the_user_is_used(upload, auth_headers):
    response = upload(auth_headers(), title="  Leave policy 2026  ")

    assert response.json()["title"] == "Leave policy 2026"


def test_file_name_and_claimed_type_are_ignored(upload, auth_headers):
    # A real PDF called notes.txt and sent as text/plain is still a PDF
    response = upload(auth_headers(), filename="notes.txt", content_type="text/plain")

    assert response.status_code == 202


def test_folder_path_and_control_characters_are_stripped_from_the_file_name(client, auth_headers):
    body, content_type = multipart_body(make_pdf(), filename="C:\\fakepath\\..\\Pay\x07\t slip\u202e.pdf")

    response = client.post("/documents", headers={**auth_headers(), "Content-Type": content_type}, content=body)

    assert response.json()["original_filename"] == "Pay slip.pdf"
    assert response.json()["title"] == "Pay slip"


# --- Refused uploads ----------------------------------------------------------------

def test_text_file_renamed_to_pdf_is_refused(db, upload, auth_headers, fake_storage):
    response = upload(auth_headers(), b"Just some text, not a PDF", filename="report.pdf")

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "not_pdf", "message": "This file is not a PDF."}}
    assert fake_storage.files == {} and count_documents(db) == 0


def test_empty_file_is_refused_as_not_pdf(upload, auth_headers):
    assert upload(auth_headers(), b"").json()["error"]["code"] == "not_pdf"


def test_too_large_by_declared_size_is_refused_before_the_body_is_read(upload, auth_headers, monkeypatch, db):
    headers = auth_headers()

    def must_not_parse(*args, **kwargs):
        raise AssertionError("the body was parsed")

    monkeypatch.setattr(uploads, "MultiPartParser", must_not_parse)

    response = upload(headers, b"%PDF-" + b"0" * (2 * MB))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert count_documents(db) == 0


def test_too_large_by_actual_bytes_is_refused_without_content_length(client, auth_headers, monkeypatch, fake_storage):
    headers = auth_headers()
    declared = []
    real_check = uploads.declared_size_too_large

    def spy(request, max_body_bytes):
        declared.append(request.headers.get("content-length"))
        return real_check(request, max_body_bytes)

    monkeypatch.setattr(uploads, "declared_size_too_large", spy)
    body, content_type = multipart_body(b"%PDF-" + b"0" * (2 * MB))
    chunks = (body[i : i + 64 * 1024] for i in range(0, len(body), 64 * 1024))

    # A generator is sent chunked, with no Content-Length to check first
    response = client.post("/documents", headers={**headers, "Content-Type": content_type}, content=chunks)

    assert declared == [None]
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert fake_storage.files == {}


def test_counting_stops_as_soon_as_the_limit_is_passed():
    pulled = []

    async def body():
        for i in range(100):
            pulled.append(i)
            yield b"x" * 10

    async def read_all():
        async for _ in uploads.limited(body(), 25):
            pass

    with pytest.raises(uploads.UploadTooLarge):
        asyncio.run(read_all())
    assert len(pulled) == 3  # 30 bytes read, then it stopped


def test_file_of_exactly_the_limit_is_accepted_and_one_byte_more_is_refused(upload, auth_headers):
    headers = auth_headers()
    exactly = b"%PDF-" + b"0" * (MB - 5)

    assert upload(headers, exactly).status_code == 202
    # Within the room left for the form, so it is the file's own size that fails
    assert upload(headers, exactly + b"1").json()["error"]["code"] == "file_too_large"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"file": "JVBERi0="}},  # not a multipart form
        {"data": {"title": "No file"}, "files": {"other": ("a.pdf", b"%PDF-1.7", "application/pdf")}},
        {"files": [("file", ("a.pdf", b"%PDF-1", "application/pdf")), ("file", ("b.pdf", b"%PDF-2", "application/pdf"))]},
        {"data": {"file": "%PDF-1.7 sent as text, not as a file"}},
    ],
    ids=["json", "no-file-field", "two-files", "file-as-text"],
)
def test_malformed_upload_requests_get_422_in_the_envelope(client, auth_headers, kwargs):
    response = client.post("/documents", headers=auth_headers(), **kwargs)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- Duplicates ------------------------------------------------------------------------

def test_same_file_twice_in_one_workspace_is_refused_with_the_existing_document(db, upload, auth_headers, fake_storage):
    headers = auth_headers()
    first = upload(headers).json()

    response = upload(headers, filename="renamed copy.pdf")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "duplicate_document",
            "message": "This file is already in your workspace.",
            "existing_document": {"id": first["id"], "status": STATUS_PROCESSING},
        }
    }
    assert count_documents(db) == 1 and len(fake_storage.files) == 1


def test_a_failed_document_still_blocks_the_same_file(db, upload, auth_headers):
    headers = auth_headers()
    first = upload(headers).json()
    db.execute(update(Document).values(status=STATUS_FAILED))
    db.commit()

    response = upload(headers)

    assert response.status_code == 409
    assert response.json()["error"]["existing_document"] == {"id": first["id"], "status": STATUS_FAILED}


def test_same_file_is_accepted_for_a_different_user(db, upload, auth_headers, fake_storage):
    alice = auth_headers("alice@example.com")
    bob = auth_headers("bob@example.com")
    pdf = make_pdf("Shared policy")

    assert upload(alice, pdf).status_code == 202
    assert upload(bob, pdf).status_code == 202
    assert count_documents(db) == 2 and len(fake_storage.files) == 2


def test_duplicate_that_slips_past_the_check_is_caught_by_the_database(db, upload, auth_headers, fake_storage, monkeypatch):
    headers = auth_headers()
    first = upload(headers).json()
    # Pretend the early check ran before the first upload committed:
    # only the unique constraint on (workspace_id, sha256) can stop it
    real_find = docs.find_duplicate
    calls = []

    def misses_once(db, workspace_id, sha256):
        calls.append(sha256)
        return None if len(calls) == 1 else real_find(db, workspace_id, sha256)

    monkeypatch.setattr(docs, "find_duplicate", misses_once)
    saved = []
    monkeypatch.setattr(fake_storage, "save", lambda *args: saved.append(args))

    response = upload(headers)

    assert response.status_code == 409
    assert response.json()["error"]["existing_document"]["id"] == first["id"]
    assert saved == []  # caught at the INSERT, before the file was saved
    assert count_documents(db) == 1


# --- The document limit -------------------------------------------------------------------

def test_eleventh_document_is_refused_and_failed_documents_do_not_count(db, upload, auth_headers, fake_storage):
    headers = auth_headers()
    for i in range(10):
        assert upload(headers, make_pdf(f"Document {i}")).status_code == 202

    response = upload(headers, make_pdf("Document 11"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "document_limit_reached"
    assert count_documents(db) == 10 and len(fake_storage.files) == 10

    # One fails: it no longer counts, so there is room again
    db.execute(
        update(Document)
        .where(Document.id == db.scalars(select(Document.id).limit(1)).one())
        .values(status=STATUS_FAILED)
    )
    db.commit()

    assert upload(headers, make_pdf("Document 11")).status_code == 202


def test_limit_is_per_workspace(upload, auth_headers):
    alice = auth_headers("alice@example.com")
    for i in range(10):
        upload(alice, make_pdf(f"Alice {i}"))

    assert upload(auth_headers("bob@example.com"), make_pdf("Bob 1")).status_code == 202


def test_two_uploads_at_once_with_nine_documents_give_exactly_one_success(db, client, upload, auth_headers, fake_storage, monkeypatch):
    headers = auth_headers()
    for i in range(9):
        assert upload(headers, make_pdf(f"Document {i}")).status_code == 202

    # Both requests reach the workspace lock at the same moment...
    barrier = threading.Barrier(2, timeout=10)
    real_get = documents_router.get_personal_workspace

    def together(db, user_id, *, lock=False):
        barrier.wait()
        return real_get(db, user_id, lock=lock)

    monkeypatch.setattr(documents_router, "get_personal_workspace", together)
    # ...and the first one is slow to save, so without the lock the second
    # would count 9 documents too and both would pass
    real_save = fake_storage.save

    def slow_save(*args):
        time.sleep(0.3)
        real_save(*args)

    monkeypatch.setattr(fake_storage, "save", slow_save)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: upload(headers, make_pdf(f"Race {i}")), range(2)))

    assert sorted(r.status_code for r in results) == [202, 403]
    assert count_documents(db) == 10 and len(fake_storage.files) == 10


# --- Storage and database failures -------------------------------------------------------

def test_storage_failure_returns_502_and_leaves_no_row(db, upload, auth_headers, fake_storage):
    headers = auth_headers()
    fake_storage.fail_on = {"save"}

    response = upload(headers)

    assert response.status_code == 502
    assert response.json() == {
        "error": {"code": "storage_error", "message": "File storage is unavailable. Please try again."}
    }
    assert count_documents(db) == 0 and fake_storage.files == {}


def test_failed_commit_after_saving_deletes_the_file(db, server_error_client, auth_headers, fake_storage, monkeypatch):
    headers = auth_headers()
    saved = []
    real_save = fake_storage.save

    def recording_save(key, data, content_type):
        saved.append(key)
        real_save(key, data, content_type)

    def broken_commit(self):
        raise OperationalError("COMMIT", None, Exception("connection lost at /secret/path"))

    monkeypatch.setattr(fake_storage, "save", recording_save)
    monkeypatch.setattr(Session, "commit", broken_commit)

    response = server_error_client.post(
        "/documents", headers=headers, files={"file": ("a.pdf", make_pdf(), "application/pdf")}
    )
    monkeypatch.undo()

    assert len(saved) == 1  # the file did reach storage...
    assert fake_storage.files == {}  # ...and was removed again
    assert count_documents(db) == 0
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "connection lost" not in response.text and "/secret/path" not in response.text


# --- What responses never contain ---------------------------------------------------------

def test_responses_never_contain_storage_key_sha256_or_workspace_ids(db, client, upload, auth_headers):
    headers = auth_headers()
    pdf = make_pdf("Private")
    responses = [upload(headers, pdf), upload(headers, pdf), upload(headers, b"not a pdf")]

    document = db.scalars(select(Document)).one()
    secrets = [document.storage_key, document.sha256, str(document.workspace_id), "storage_key", "sha256", "workspace_id"]
    for response in responses:
        for secret in secrets:
            assert secret not in response.text


# --- Who can upload -----------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-token"}], ids=["no-token", "bad-token"])
def test_upload_without_login_gets_401_before_the_body_is_read(client, db, monkeypatch, headers):
    async def must_not_read(*args, **kwargs):
        raise AssertionError("the body was read")

    monkeypatch.setattr(documents_router, "read_upload", must_not_read)

    response = client.post("/documents", headers=headers, files={"file": ("a.pdf", make_pdf(), "application/pdf")})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert count_documents(db) == 0


def test_user_without_a_workspace_membership_cannot_upload(db, upload, auth_headers, fake_storage):
    headers = auth_headers()
    db.execute(delete(WorkspaceMember))
    db.commit()

    response = upload(headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"
    assert count_documents(db) == 0 and fake_storage.files == {}


# --- Helpers ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\amy\\Desktop\\cv.pdf", "cv.pdf"),
        ("line\nbreak\t.pdf", "linebreak.pdf"),
        ("evil\u202efdp.exe", "evilfdp.exe"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("", "document.pdf"),
        (None, "document.pdf"),
        ("..", "document.pdf"),
        ("dir/", "document.pdf"),
        ("a" * 300 + ".pdf", "a" * 255),
    ],
)
def test_clean_filename(raw, expected):
    assert docs.clean_filename(raw) == expected


@pytest.mark.parametrize(
    "title, filename, expected",
    [
        (None, "Handbook.pdf", "Handbook"),
        (None, "SHOUTING.PDF", "SHOUTING"),
        (None, "notes.txt", "notes.txt"),
        ("", "Handbook.pdf", "Handbook"),
        ("   ", "Handbook.pdf", "Handbook"),
        ("My\x07 title", "Handbook.pdf", "My title"),
        (None, ".pdf", "Untitled document"),
        (None, "b" * 300 + ".pdf", "b" * 200),
        ("t" * 250, "Handbook.pdf", "t" * 200),
    ],
)
def test_clean_title(title, filename, expected):
    assert docs.clean_title(title, filename) == expected
