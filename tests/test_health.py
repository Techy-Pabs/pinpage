def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "pinpage"}


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_root_is_hidden_from_swagger(client):
    assert "/" not in client.get("/openapi.json").json()["paths"]


def test_unknown_path_uses_the_error_envelope(client):
    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_wrong_method_uses_the_error_envelope(client):
    response = client.delete("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
