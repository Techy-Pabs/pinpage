# Pinpage

An AI document assistant for HR teams and schools. People upload their documents (handbooks, policies, guides) and ask questions about them, with answers that point back to the exact page.

This repository is at **Milestone 1, Slice 3**: the foundation, workspaces, the file storage layer and PDF upload. It has registration (which also creates a personal workspace), login, and uploading a PDF to Cloudflare R2 with every check and limit. Text extraction arrives in Slice 4, so uploaded documents stay `processing` for now. There is no AI yet. The plan for Milestone 1 is in [docs/specs/milestone-1.md](docs/specs/milestone-1.md).

## Stack

FastAPI, SQLAlchemy 2, PostgreSQL 16 (with pgvector, for later milestones), Alembic, Argon2 password hashing, JWT login tokens, Pytest, Docker Compose and GitHub Actions.

## Run it locally

You need Docker.

```sh
cp .env.example .env      # then change JWT_SECRET and fill in your R2 values
docker compose up --build
```

The app refuses to start if `DATABASE_URL`, `JWT_SECRET` or any `R2_` value is missing or empty. It doesn't contact R2 at startup; the connection is made on first use.

Migrations run automatically before the API starts. Then open http://localhost:8001/docs for Swagger.

The API listens on port **8001** and the database on **5433** on your machine, so Pinpage can run next to ShopAssist (which uses 8000 and 5432). Inside Docker they talk on the usual ports.

## Run the tests

```sh
docker compose run --rm --build tests
```

Tests use a separate database (`pinpage_test`), created automatically and emptied before every test. GitHub Actions runs the same command on every push, using only the placeholder values in `.env.example`.

No test contacts R2. The tests service replaces the R2 values from `.env` with placeholders (see `docker-compose.yml`), every test stores files in memory with `FakeStorage`, and any unstubbed call that reaches botocore's network layer fails the test.

## Check the R2 connection (manual)

Once your real R2 values are in `.env`, run this by hand to prove they work:

```sh
docker compose run --rm --no-deps --build api python scripts/check_storage.py
```

It saves one small file under `healthcheck/` in the bucket, reads it back, downloads it through a signed link, deletes it and checks it is gone, printing PASS or FAIL for each step. It never prints keys, links or secrets. It is not part of the automated tests.

## Endpoints so far

| Method and path | What it does |
| --- | --- |
| POST /auth/register | Creates an account and its personal workspace, in one transaction (201; 409 if the email is taken) |
| POST /auth/login | Returns a JWT |
| GET /auth/me | Returns the logged in user |
| POST /documents | Uploads a PDF as a multipart form (`file`, optional `title`). 202 with the document, status `processing`. Errors: 400 `not_pdf`, 403 `document_limit_reached`, 409 `duplicate_document` (with the existing document's id and status), 413 `file_too_large`, 502 `storage_error` |
| GET /health | Health check |

Every error uses one shape: `{"error": {"code": "...", "message": "..."}}`.

## Making an admin

Accounts are created by registering. To make one an admin (or undo it):

```sh
docker compose exec api python -m app.cli.users promote someone@example.com
docker compose exec api python -m app.cli.users demote someone@example.com
```

The change takes effect on the user's next request; no new login is needed.

## Security notes

- Passwords are hashed with Argon2 and never returned or echoed in error messages.
- Every login or token failure gets the same 401, and an unknown email costs as much time as a wrong password, so the API doesn't reveal which emails have accounts.
- Admin rights are read from the database on every request, not from the token.
- All ids are UUIDs, so they cannot be guessed by counting.
- Real secrets live only in `.env`, which is never committed.
- The R2 bucket is private. Files will be reached only through signed links that expire after `SIGNED_URL_SECONDS` (5 minutes by default).
- Storage keys are built only from UUIDs (`workspaces/{workspace_id}/documents/{document_id}.pdf`), never from a user's file name.
- Storage errors reach callers as one safe message; logs record only the operation and the error code, never keys, links or secrets.
- Uploads are checked by their own bytes (`%PDF-`), never by file name or claimed type. The size is checked twice: the declared size before anything is read, then the real bytes as they arrive, stopping as soon as the limit is passed.
- The workspace row is locked while an upload checks the document limit, so two uploads at once can't both squeeze past it.
- A failed upload leaves nothing behind: no row if storage fails, and no file if the database fails after the file was saved.
- Document responses never contain storage keys, fingerprints (sha256) or workspace ids.

## Licence

MIT. See [LICENSE](LICENSE).
