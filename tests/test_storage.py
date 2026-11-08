"""The storage layer. Nothing here contacts R2: R2Storage is tested offline
with botocore's Stubber, and conftest blocks botocore's network layer."""

import io
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from fastapi import APIRouter, Depends

from app.core.config import settings
from app.dependencies import get_storage
from app.main import app
from app.services.storage import (
    UNAVAILABLE_MESSAGE,
    FakeStorage,
    ObjectNotFound,
    R2Storage,
    Storage,
    StorageError,
    default_storage,
    document_key,
)

SECRET = "test-secret-do-not-leak"
KEY = "workspaces/11111111-1111-1111-1111-111111111111/documents/22222222-2222-2222-2222-222222222222.pdf"
PDF = b"%PDF-1.7 test bytes"


@pytest.fixture
def r2():
    return R2Storage(account_id="acct123", access_key_id="AKTEST", secret_access_key=SECRET, bucket="pinpage-dev")


@pytest.fixture
def stubber(r2):
    with Stubber(r2.client) as stub:
        yield stub
        stub.assert_no_pending_responses()  # every expected call really happened


# --- Storage keys -----------------------------------------------------------

def test_document_key_format():
    workspace_id, document_id = uuid.uuid4(), uuid.uuid4()

    assert document_key(workspace_id, document_id) == f"workspaces/{workspace_id}/documents/{document_id}.pdf"


@pytest.mark.parametrize(
    "workspace_id, document_id",
    [
        ("../../etc", uuid.uuid4()),
        (uuid.uuid4(), "report.pdf"),
        (str(uuid.uuid4()), uuid.uuid4()),  # even a valid UUID string: callers pass UUID objects
        (uuid.uuid4(), 42),
        (None, uuid.uuid4()),
        (uuid.uuid4(), ""),
    ],
)
def test_document_key_rejects_anything_but_uuids(workspace_id, document_id):
    with pytest.raises(ValueError, match="UUIDs only"):
        document_key(workspace_id, document_id)


# --- FakeStorage ------------------------------------------------------------

def test_fake_storage_saves_reads_and_deletes():
    fake = FakeStorage()

    fake.save(KEY, PDF, "application/pdf")
    assert fake.read(KEY) == PDF
    assert fake.content_types[KEY] == "application/pdf"

    fake.delete(KEY)
    with pytest.raises(ObjectNotFound):
        fake.read(KEY)


def test_fake_storage_delete_of_missing_key_succeeds():
    FakeStorage().delete(KEY)  # no error


def test_fake_storage_signed_url_and_expiry_check():
    fake = FakeStorage()

    assert fake.signed_url(KEY, 300).endswith(f"{KEY}?expires=300")
    with pytest.raises(ValueError):
        fake.signed_url(KEY, 0)


@pytest.mark.parametrize("operation", ["save", "read", "delete", "signed_url"])
def test_fake_storage_can_simulate_an_outage(operation):
    fake = FakeStorage()
    fake.save(KEY, PDF, "application/pdf")
    fake.fail_on = {operation}
    call = {
        "save": lambda: fake.save(KEY, PDF, "application/pdf"),
        "read": lambda: fake.read(KEY),
        "delete": lambda: fake.delete(KEY),
        "signed_url": lambda: fake.signed_url(KEY, 300),
    }[operation]

    with pytest.raises(StorageError, match=UNAVAILABLE_MESSAGE):
        call()


def test_both_versions_offer_the_same_interface():
    assert issubclass(R2Storage, Storage) and issubclass(FakeStorage, Storage)


# --- R2Storage, offline with Stubber ------------------------------------------

def test_r2_client_is_created_lazily(r2):
    assert r2._client is None  # building the storage doesn't contact or even configure R2


def test_r2_client_uses_the_r2_endpoint_and_region(r2):
    assert r2.client.meta.endpoint_url == "https://acct123.r2.cloudflarestorage.com"
    assert r2.client.meta.region_name == "auto"
    config = r2.client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (5, 30)
    assert config.retries["total_max_attempts"] == 3


def test_r2_save_sends_bucket_key_body_and_type(r2, stubber):
    stubber.add_response(
        "put_object", {},
        expected_params={"Bucket": "pinpage-dev", "Key": KEY, "Body": PDF, "ContentType": "application/pdf"},
    )

    r2.save(KEY, PDF, "application/pdf")


def test_r2_read_returns_the_bytes(r2, stubber):
    stubber.add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(PDF), len(PDF))},
        expected_params={"Bucket": "pinpage-dev", "Key": KEY},
    )

    assert r2.read(KEY) == PDF


def test_r2_read_of_missing_key_raises_object_not_found(r2, stubber):
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with pytest.raises(ObjectNotFound):
        r2.read(KEY)


def test_r2_delete_sends_bucket_and_key(r2, stubber):
    stubber.add_response("delete_object", {}, expected_params={"Bucket": "pinpage-dev", "Key": KEY})

    r2.delete(KEY)


@pytest.mark.parametrize("code, status", [("NoSuchKey", 404), ("404", 404)])
def test_r2_delete_of_missing_key_counts_as_success(r2, stubber, code, status):
    stubber.add_client_error("delete_object", service_error_code=code, http_status_code=status)

    r2.delete(KEY)  # no error


@pytest.mark.parametrize("operation", ["put_object", "get_object", "delete_object"])
def test_r2_errors_become_a_safe_storage_error(r2, stubber, operation, caplog):
    stubber.add_client_error(
        operation, service_error_code="AccessDenied", http_status_code=403,
        service_message=f"Denied for {KEY} using {SECRET}",
    )
    call = {
        "put_object": lambda: r2.save(KEY, PDF, "application/pdf"),
        "get_object": lambda: r2.read(KEY),
        "delete_object": lambda: r2.delete(KEY),
    }[operation]

    with pytest.raises(StorageError) as error:
        call()

    assert type(error.value) is StorageError
    assert str(error.value) == UNAVAILABLE_MESSAGE
    assert error.value.__cause__ is None and error.value.__suppress_context__  # boto3's error isn't chained on
    assert "AccessDenied" in caplog.text  # the log says what went wrong...
    assert KEY not in caplog.text and SECRET not in caplog.text  # ...but not the key or the secret


def test_r2_network_errors_become_a_storage_error(r2, monkeypatch):
    def unreachable(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://acct123.r2.cloudflarestorage.com")

    monkeypatch.setattr(r2.client, "put_object", unreachable)

    with pytest.raises(StorageError, match=UNAVAILABLE_MESSAGE):
        r2.save(KEY, PDF, "application/pdf")


def test_r2_timeout_while_reading_the_body_becomes_a_storage_error(r2, monkeypatch):
    # The response headers arrive, then the connection stalls mid-download
    class StalledBody:
        def read(self):
            raise ReadTimeoutError(endpoint_url="https://acct123.r2.cloudflarestorage.com")

    monkeypatch.setattr(r2.client, "get_object", lambda **kwargs: {"Body": StalledBody()})

    with pytest.raises(StorageError, match=UNAVAILABLE_MESSAGE):
        r2.read(KEY)


def test_an_unstubbed_r2_call_never_reaches_the_network(r2):
    # conftest's safety net stops it before anything is sent
    with pytest.raises(RuntimeError, match="tried to reach the network"):
        r2.save(KEY, PDF, "application/pdf")


# --- Signed links -------------------------------------------------------------

def test_signed_url_contains_bucket_key_and_expiry_but_never_the_secret(r2):
    url = r2.signed_url(KEY, 300)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "acct123.r2.cloudflarestorage.com"
    assert parsed.path == f"/pinpage-dev/{KEY}"
    assert query["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Signature" in query
    assert SECRET not in url


@pytest.mark.parametrize("bad", [0, -5, 7 * 24 * 3600 + 1])
def test_signed_url_rejects_impossible_expiry(r2, bad):
    with pytest.raises(ValueError):
        r2.signed_url(KEY, bad)


def test_r2_storage_repr_hides_the_keys(r2):
    assert SECRET not in repr(r2) and "AKTEST" not in repr(r2)


# --- The get_storage dependency ----------------------------------------------

def test_get_storage_gives_one_lazy_r2_storage_from_settings():
    storage = get_storage()

    assert isinstance(storage, R2Storage)
    assert storage is default_storage()  # one shared instance
    assert storage.bucket == settings.r2_bucket
    assert storage._client is None  # the app starts without contacting R2


def test_routes_get_fake_storage_in_tests(client, fake_storage):
    router = APIRouter()

    @router.get("/test-only/storage")
    def which_storage(storage: Storage = Depends(get_storage)):
        return {"storage": type(storage).__name__, "same": storage is fake_storage}

    app.include_router(router)
    try:
        assert client.get("/test-only/storage").json() == {"storage": "FakeStorage", "same": True}
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != "/test-only/storage"]
        app.openapi_schema = None
