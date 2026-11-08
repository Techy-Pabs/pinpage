"""Manual check that the R2 settings work against the real bucket.

Run it by hand (see the README), never in the automated tests:

    docker compose run --rm --no-deps --build api python scripts/check_storage.py

It saves one small file under healthcheck/, reads it back, downloads it
through a signed link, deletes it and checks it is gone. It prints PASS or
FAIL for each step and never prints keys, links or secrets.
"""

import secrets
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Lets "python scripts/check_storage.py" import the app package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services.storage import ObjectNotFound, R2Storage, StorageError  # noqa: E402

DOWNLOAD_TIMEOUT_SECONDS = 15


def describe(error: Exception) -> str:
    """A safe one-line reason: our own StorageError message, an HTTP status,
    or just the error's type. Never the raw text, which could contain a link."""
    if isinstance(error, StorageError):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


def main() -> int:
    storage = R2Storage.from_settings(settings)
    key = f"healthcheck/{uuid.uuid4()}.txt"
    data = f"pinpage storage check {secrets.token_hex(8)}\n".encode()
    results: list[bool] = []
    saved = deleted = False

    def step(name: str, check) -> bool:
        try:
            ok, reason = check()
        except Exception as error:  # report every failure as FAIL, never a traceback
            ok, reason = False, describe(error)
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" ({reason})"))
        results.append(ok)
        return ok

    print(f"Checking bucket {storage.bucket}")

    def save():
        storage.save(key, data, "text/plain")
        return True, ""

    saved = step("save a test file", save)

    if saved:
        step("read it back and compare", lambda: (storage.read(key) == data, "content differs"))

        def via_signed_link():
            url = storage.signed_url(key, 60)
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                return response.read() == data, "content differs"

        step("create a signed link and download through it", via_signed_link)

        def delete():
            storage.delete(key)
            return True, ""

        deleted = step("delete the test file", delete)

        def confirm_gone():
            try:
                storage.read(key)
            except ObjectNotFound:
                return True, ""
            return False, "the file is still there"

        if deleted:
            step("confirm it is gone", confirm_gone)

    if saved and not deleted:
        print("Note: the test file may still be in the bucket under healthcheck/. Delete it there.")

    passed = all(results) and len(results) == 5
    print("Storage check passed." if passed else "Storage check FAILED.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
