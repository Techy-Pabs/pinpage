"""Swagger (/docs) describes every endpoint."""

from app.main import app

EXPECTED = {
    ("post", "/auth/register"),
    ("post", "/auth/login"),
    ("get", "/auth/me"),
    ("get", "/health"),
    ("post", "/documents"),
    ("get", "/documents"),
    ("get", "/documents/{document_id}"),
    ("get", "/documents/{document_id}/pages/{page_number}"),
    ("get", "/documents/{document_id}/file"),
    ("delete", "/documents/{document_id}"),
}


def operations():
    app.openapi_schema = None  # build it fresh
    for path, methods in app.openapi()["paths"].items():
        for method, operation in methods.items():
            yield method, path, operation


def test_every_endpoint_is_listed():
    assert {(method, path) for method, path, _ in operations()} == EXPECTED


def test_every_endpoint_has_a_summary_and_a_description():
    for method, path, operation in operations():
        assert operation.get("summary"), f"{method} {path} has no summary"
        assert operation.get("description"), f"{method} {path} has no description"


def test_upload_shows_its_form_fields():
    upload = app.openapi()["paths"]["/documents"]["post"]
    schema = upload["requestBody"]["content"]["multipart/form-data"]["schema"]

    assert set(schema["properties"]) == {"file", "title"}
    assert schema["required"] == ["file"]
