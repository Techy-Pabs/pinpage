"""Where original PDFs are kept.

One interface, two versions:
- R2Storage talks to Cloudflare R2 through boto3 (R2 speaks the S3 API).
- FakeStorage keeps files in memory, for tests.

Callers only ever see StorageError (or its ObjectNotFound subclass), whose
messages are written by us: never a raw boto3 error, a key or a secret.
"""

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

# Error codes R2/S3 use for "there is no such object"
MISSING_OBJECT_CODES = {"NoSuchKey", "404", "NotFound"}

UNAVAILABLE_MESSAGE = "File storage is unavailable. Please try again."


class StorageError(Exception):
    """Storage failed. The message is safe to show or log."""

    def __init__(self, message: str = UNAVAILABLE_MESSAGE):
        super().__init__(message)


class ObjectNotFound(StorageError):
    """The key doesn't exist in storage."""

    def __init__(self):
        super().__init__("The file was not found in storage.")


def document_key(workspace_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """Storage key for a document's original PDF.

    Only real UUID objects are accepted (not strings), so nothing a user
    typed, such as a file name or "../", can ever end up in a key.
    """
    for value in (workspace_id, document_id):
        if not isinstance(value, uuid.UUID):
            raise ValueError("Storage keys are built from UUIDs only")
    return f"workspaces/{workspace_id}/documents/{document_id}.pdf"


class Storage(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def read(self, key: str) -> bytes:
        """The file's bytes. Raises ObjectNotFound if the key doesn't exist."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the file. A key that doesn't exist counts as deleted."""

    @abstractmethod
    def signed_url(self, key: str, expires_seconds: int) -> str:
        """A link anyone holding it can use to download the file, until it expires."""


def _check_expiry(expires_seconds: int) -> None:
    if not 0 < expires_seconds <= 7 * 24 * 3600:
        raise ValueError("expires_seconds must be between 1 second and 7 days")


class R2Storage(Storage):
    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str):
        self.bucket = bucket
        self._endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = None
        self._client_lock = threading.Lock()

    @classmethod
    def from_settings(cls, config: Settings) -> "R2Storage":
        return cls(
            account_id=config.r2_account_id,
            access_key_id=config.r2_access_key_id,
            secret_access_key=config.r2_secret_access_key.get_secret_value(),
            bucket=config.r2_bucket,
        )

    def __repr__(self) -> str:
        # Never show the keys, even when debugging
        return f"R2Storage(bucket={self.bucket!r})"

    @property
    def client(self):
        """The boto3 client, created on first use, so the app starts without contacting R2.

        Creating a client is not thread safe, and sync FastAPI routes run in
        a thread pool, so only one thread creates it; the client itself is
        thread safe once created.
        """
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = boto3.session.Session().client(
                        "s3",
                        endpoint_url=self._endpoint,
                        aws_access_key_id=self._access_key_id,
                        aws_secret_access_key=self._secret_access_key,
                        region_name="auto",
                        config=Config(
                            signature_version="s3v4",
                            # Fail fast instead of hanging a request on a dead connection
                            connect_timeout=5,
                            read_timeout=30,
                            # 3 attempts in total, with backoff between them
                            retries={"total_max_attempts": 3, "mode": "standard"},
                            # https://{account}.r2.cloudflarestorage.com/{bucket}/{key}
                            s3={"addressing_style": "path"},
                        ),
                    )
        return self._client

    def _fail(self, operation: str, error: Exception) -> StorageError:
        # Log the operation and the error type or S3 code only: no key, no
        # message text (which could echo the request), no credentials
        code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
        logger.warning("Storage %s failed: %s", operation, code or type(error).__name__)
        return StorageError()

    def save(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        except (BotoCoreError, ClientError) as error:
            raise self._fail("save", error) from None

    def read(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            # Reading the body is network I/O too, so it stays inside the try
            return response["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in MISSING_OBJECT_CODES:
                raise ObjectNotFound() from None
            raise self._fail("read", error) from None
        except BotoCoreError as error:
            raise self._fail("read", error) from None

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            # S3 already answers success for a missing key; this covers
            # stores that say "not found" instead
            if error.response.get("Error", {}).get("Code") in MISSING_OBJECT_CODES:
                return
            raise self._fail("delete", error) from None
        except BotoCoreError as error:
            raise self._fail("delete", error) from None

    def signed_url(self, key: str, expires_seconds: int) -> str:
        _check_expiry(expires_seconds)
        try:
            # Signed locally with the secret; no request is sent to R2
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except (BotoCoreError, ClientError) as error:
            raise self._fail("signed_url", error) from None


class FakeStorage(Storage):
    """In-memory storage for tests. Never touches the network.

    Put an operation name in fail_on (e.g. {"delete"}) to make it raise
    StorageError, like an R2 outage.
    """

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.fail_on: set[str] = set()

    def _maybe_fail(self, operation: str) -> None:
        if operation in self.fail_on:
            raise StorageError()

    def save(self, key: str, data: bytes, content_type: str) -> None:
        self._maybe_fail("save")
        self.files[key] = bytes(data)
        self.content_types[key] = content_type

    def read(self, key: str) -> bytes:
        self._maybe_fail("read")
        if key not in self.files:
            raise ObjectNotFound()
        return self.files[key]

    def delete(self, key: str) -> None:
        self._maybe_fail("delete")
        self.files.pop(key, None)
        self.content_types.pop(key, None)

    def signed_url(self, key: str, expires_seconds: int) -> str:
        self._maybe_fail("signed_url")
        _check_expiry(expires_seconds)
        return f"https://fake-storage.invalid/{key}?expires={expires_seconds}"


@lru_cache(maxsize=1)
def default_storage() -> R2Storage:
    """The app's one R2Storage, built from settings on first use."""
    return R2Storage.from_settings(settings)
