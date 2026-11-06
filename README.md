# Pinpage

An AI document assistant for HR teams and schools. People upload their documents (handbooks, policies, guides) and ask questions about them, with answers that point back to the exact page.

This repository is at **Milestone 1, Slice 0**: the foundation. It has registration, login and the database, with no documents or AI yet. The plan for Milestone 1 is in [docs/specs/milestone-1.md](docs/specs/milestone-1.md).

## Stack

FastAPI, SQLAlchemy 2, PostgreSQL 16 (with pgvector, for later milestones), Alembic, Argon2 password hashing, JWT login tokens, Pytest, Docker Compose and GitHub Actions.

## Run it locally

You need Docker.

```sh
cp .env.example .env      # then change JWT_SECRET to a long random value
docker compose up --build
```

Migrations run automatically before the API starts. Then open http://localhost:8001/docs for Swagger.

The API listens on port **8001** and the database on **5433** on your machine, so Pinpage can run next to ShopAssist (which uses 8000 and 5432). Inside Docker they talk on the usual ports.

## Run the tests

```sh
docker compose run --rm --build tests
```

Tests use a separate database (`pinpage_test`), created automatically and emptied before every test. GitHub Actions runs the same command on every push, using only the placeholder values in `.env.example`.

## Endpoints so far

| Method and path | What it does |
| --- | --- |
| POST /auth/register | Creates an account (201; 409 if the email is taken) |
| POST /auth/login | Returns a JWT |
| GET /auth/me | Returns the logged in user |
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

## Licence

MIT. See [LICENSE](LICENSE).
