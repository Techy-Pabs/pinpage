"""Reading an uploaded file without trusting the client about its size.

FastAPI's own File() parameters read the whole request before any of our
code runs. The upload route reads the body itself instead, so it can:
1. refuse a request whose declared size (Content-Length) is too big, before
   reading any of it, and
2. count the bytes as they arrive, stopping as soon as the limit is passed,
   for clients that send no Content-Length or lie about it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

# Room for the multipart boundaries, part headers and the title field
# around the file itself
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadTooLarge(MultiPartException):
    """The body passed the limit. A MultiPartException, so Starlette's parser
    closes any temporary files it opened before passing it on."""

    def __init__(self):
        super().__init__("The upload is too large")


class UploadInvalid(Exception):
    """The request isn't a multipart form with one file in "file"."""


@dataclass
class Upload:
    data: bytes
    filename: str | None
    title: str | None


def declared_size_too_large(request: Request, max_body_bytes: int) -> bool:
    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        return False  # no usable number: the byte count below still applies
    return int(declared) > max_body_bytes


async def limited(stream: AsyncIterator[bytes], max_body_bytes: int) -> AsyncIterator[bytes]:
    """Passes the body on chunk by chunk, stopping once it passes the limit."""
    received = 0
    async for chunk in stream:
        received += len(chunk)
        if received > max_body_bytes:
            raise UploadTooLarge()
        yield chunk


async def read_upload(request: Request, max_file_bytes: int) -> Upload:
    """The file and optional title from a multipart form.

    Raises UploadTooLarge (before reading anything if the declared size is
    too big) or UploadInvalid.
    """
    max_body_bytes = max_file_bytes + MULTIPART_OVERHEAD_BYTES
    if declared_size_too_large(request, max_body_bytes):
        raise UploadTooLarge()

    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise UploadInvalid()

    parser = MultiPartParser(
        request.headers,
        limited(request.stream(), max_body_bytes),
        max_files=1,
        max_fields=1,
        max_part_size=MULTIPART_OVERHEAD_BYTES,
    )
    try:
        form = await parser.parse()
    except UploadTooLarge:
        raise
    except MultiPartException:
        # Malformed body, a missing boundary, or more than one file or field
        raise UploadInvalid() from None

    try:
        file = form.get("file")
        title = form.get("title")
        if not isinstance(file, UploadFile) or not (title is None or isinstance(title, str)):
            raise UploadInvalid()
        data = await file.read()
        # The body limit includes room for the form around the file; the
        # file itself must fit the real limit
        if len(data) > max_file_bytes:
            raise UploadTooLarge()
        return Upload(data=data, filename=file.filename, title=title)
    finally:
        await form.close()
