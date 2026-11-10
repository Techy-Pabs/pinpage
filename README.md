# Pinpage

An AI document assistant for HR teams and schools. People upload their documents (handbooks, policies, guides) and ask questions about them, with answers that point back to the exact page.

This repository has **Milestones 1 and 2**. You can register, upload a PDF, watch it move from `processing` to `ready`, read its text page by page, see its sections, open the original through a short-lived link, and delete it. Then you can start a conversation with the document and ask questions: every answer cites the pages and section it came from, and if the document doesn't cover the question, Pinpage says so instead of guessing. Nobody else can reach your documents or your conversations. The plans are in [docs/specs/milestone-1.md](docs/specs/milestone-1.md) and [docs/specs/milestone-2.md](docs/specs/milestone-2.md).

## What it does

- **Accounts.** Register and log in with an email and password. Registering also creates your personal workspace.
- **Upload.** Send a PDF. The API checks it within a second or two and answers `202` with status `processing`.
- **Processing.** In the background, in three stages:
  1. Read the file from storage and extract the text of each page.
  2. Find the sections (from the PDF's bookmarks, else numbered headings such as `4.2` or `Section 4`, else one section) and split each into chunks of about 1,000 characters that remember their pages and section. No AI. A section ends on the page before when the next one starts at the top of a page. If the first section starts after page 1, the pages before it (cover, contents) become a "Front matter" section, which is never used to answer questions.
  3. Create an embedding for each chunk through OpenRouter, in batches, paced to stay under the free limits.

  Then the document is `ready`. If it can't be, it is `failed` with a reason you can show the user: too many pages, password protected, a scan with no text, a damaged file, an interruption, or the AI being unavailable.
- **Retry.** A document that failed through no fault of the file (`interrupted` or `ai_unavailable`) can be retried. It resumes from the first unfinished stage: if only the embeddings failed, only they are redone.
- **Library.** List your documents, check one's status, read any page, list its sections, get a link to the original file, or delete it.
- **Pages and sections.** Ask about pages ("What does page 12 say?", "pages 10 to 11", up to 3 at a time) and Pinpage reads those whole pages; ask for a summary of a section ("Summarise section 4", "Give me an overview of probation") and it reads the whole section with its subsections, in parts if it is long. Page numbers are the PDF's own page positions (page 1 is the first page of the file), as a PDF viewer shows them. Recognising these requests costs no AI request, and a page out of range or a section that doesn't exist is answered without one.
- **Questions.** Start a conversation with a ready document and ask questions about it. Pinpage finds the most relevant chunks of that document only, sends them to the chat model as numbered excerpts (`[S1]`, `[S2]`...) with their pages and section, and keeps only citations that point to a real excerpt. An answer with no valid citation, or a question no chunk is close enough to, gets the fixed not found answer (the second without asking the chat model at all). Conversations are private to the person who started them.
- **Ownership.** Every document belongs to a workspace, and every query checks your membership inside the query itself. Someone else's document gets the same `404` as one that doesn't exist.

## Stack

FastAPI, SQLAlchemy 2, PostgreSQL 16 with pgvector (`halfvec` embeddings with an HNSW cosine index), Alembic, pypdf, LangChain's text splitter (only that), OpenRouter through the openai client, Cloudflare R2 through boto3, Argon2 password hashing, JWT login tokens, Pytest, Docker Compose and GitHub Actions.

## Run it locally

You need Docker, a private Cloudflare R2 bucket with an API token that can read and write it, and an OpenRouter API key.

```sh
cp .env.example .env      # then set JWT_SECRET, your four R2 values and OPENROUTER_API_KEY
docker compose up --build
```

Migrations run automatically before the API starts. Open http://localhost:8001/docs for Swagger: register, log in, click **Authorize** and paste the token, then try the document endpoints.

The app refuses to start if `DATABASE_URL`, `JWT_SECRET` or any `R2_` value is missing or empty. Without `OPENROUTER_API_KEY` it starts, but documents fail with `ai_unavailable` until the key is set (then retry them). It doesn't contact R2 or OpenRouter at startup; connections are made on first use.

The API listens on port **8001** and the database on **5433** on your machine, so Pinpage can run next to ShopAssist (which uses 8000 and 5432). Inside Docker they use the usual ports.

## Endpoints

Every document endpoint needs a login (`Authorization: Bearer <token>`). Swagger at `/docs` describes each one.

| Method and path | What it does | Success | Main errors |
| --- | --- | --- | --- |
| POST /auth/register | Creates an account, its personal workspace and the owner membership, in one transaction | 201 | 409 `email_taken`, 422 |
| POST /auth/login | Returns a JWT | 200 | 401 `invalid_credentials` |
| GET /auth/me | The logged in user | 200 | 401 |
| POST /documents | Uploads a PDF (multipart form: `file`, optional `title`) | 202, status `processing` | 400 `not_pdf`, 403 `document_limit_reached`, 409 `duplicate_document`, 413 `file_too_large`, 502 `storage_error` |
| GET /documents | Your documents, newest first | 200 | 401 |
| GET /documents/{id} | One document and its status | 200 | 404 `document_not_found` |
| GET /documents/{id}/pages/{page_number} | The text of one page (pages start at 1) | 200 | 404 `document_not_found` or `page_not_found`, 409 `document_not_ready` |
| GET /documents/{id}/sections | The detected sections in order, with their pages and `kind` (`body` or `front_matter`) | 200 | 404, 409 `document_not_ready` |
| POST /documents/{id}/retry | Restarts processing after `interrupted` or `ai_unavailable`, from the first unfinished stage | 202, status `processing` | 404, 409 `retry_not_allowed`, 403 `document_limit_reached` |
| GET /documents/{id}/file | A signed link to the original PDF, with `expires_in` | 200 | 404, 409 `document_not_ready`, 502 |
| POST /documents/{id}/conversations | Starts a conversation with a ready document | 201 | 404, 409 `document_not_ready` |
| GET /documents/{id}/conversations | Your conversations with this document, newest first | 200 | 404 |
| GET /conversations/{id} | One conversation with its messages, oldest first | 200 | 404 `conversation_not_found` |
| POST /conversations/{id}/messages | Asks a question (`{"content": "..."}`, up to 2,000 characters) and returns the answer with its citations | 201 | 404, 422, 429 `daily_limit_reached`, 503 `ai_unavailable` (nothing saved) |
| DELETE /conversations/{id} | Deletes a conversation and its messages | 204 | 404 |
| DELETE /documents/{id} | Deletes the file, the document and its pages, in any status | 204 | 404, 502 `storage_error` (the document is kept, so you can retry) |
| GET /health | Health check | 200 | |

Every error uses one shape: `{"error": {"code": "...", "message": "..."}}`. A `duplicate_document` error also carries `existing_document` with that document's `id` and `status`.

An answer is a message with `mode` (`question`, `page` or `section`), `outcome` (`answered` or `not_found`) and `citations`, each with `marker`, `page_start`, `page_end`, `section_number`, `section_title` and a `snippet` of up to 200 characters. The not found answer is always: *I couldn't find this in the document. Try rephrasing, or ask about a specific section.*

A document in a response has: `id`, `title`, `original_filename`, `size_bytes`, `page_count`, `status`, `error_code`, `error_message`, `created_at` and `processed_at`. It never includes the storage key, the file's fingerprint or workspace ids.

### Why a document fails

| error_code | When |
| --- | --- |
| `too_many_pages` | More than `MAX_PAGES` pages |
| `encrypted` | Needs a password to open (a PDF that opens with an empty password is read normally) |
| `no_text_layer` | At least 80% of pages have fewer than 20 characters: probably a scan |
| `unreadable` | The PDF is damaged: pypdf can't open it |
| `interrupted` | The file couldn't be read from storage, the database failed during processing, or the document was still processing `PROCESSING_TIMEOUT_MINUTES` after its upload or retry (e.g. because the server restarted; checked when the app starts). Can be retried |
| `ai_unavailable` | Embeddings couldn't be created (OpenRouter down, rate limited, or no key). Can be retried |

`error_message` is always a sentence written by us, never exception text. A document that failed because of its file keeps nothing: delete it and upload a fixed file. One that failed with `interrupted` or `ai_unavailable` keeps the stages it finished, so `POST /documents/{id}/retry` carries on from there.

## Limits

All set in `.env`, so they change without code changes:

| Setting | Default | Meaning |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | 20 | Largest file. At most 100, since a file is held in memory while it is checked |
| `MAX_PAGES` | 100 | Longest document |
| `MAX_DOCUMENTS_PER_WORKSPACE` | 10 | Processing and ready documents per workspace (failed ones don't count) |
| `SIGNED_URL_SECONDS` | 300 | How long a link to an original file works |
| `PROCESSING_TIMEOUT_MINUTES` | 15 | When a document still processing counts as interrupted |
| `JWT_EXPIRE_MINUTES` | 30 | How long a login lasts |
| `CHUNK_SIZE`, `CHUNK_OVERLAP` | 1000, 150 | Characters per chunk, and characters neighbouring chunks share |
| `EMBEDDING_BATCH_SIZE` | 64 | Chunks per embedding request |
| `AI_REQUESTS_PER_MINUTE` | 15 | AI requests a minute at most, safely under OpenRouter's 20. Each request gets at most 2 attempts |
| `RETRIEVAL_TOP_K` | 6 | Excerpts of the document sent with a question at most |
| `RETRIEVAL_MIN_SIMILARITY` | 0.30 | How similar an excerpt must be to be used (ShopAssist's tuned value) |
| `DAILY_QUESTIONS_PER_USER` | 20 | Questions per user per day, counted in UTC (429 after that) |
| `MAX_PAGES_PER_QUESTION` | 3 | Pages read for one page question; more are cut, with a note in the answer |
| `SUMMARY_PART_CHARS` | 12000 | Characters per summary part; a longer section is summarised in parts, then combined |
| `SUMMARY_MAX_PARTS` | 4 | Parts one summary may use (so at most 5 requests); above that, Pinpage suggests the subsections instead. A summary counts as one question |

The AI models and the other AI settings are in `.env.example`, with the values ShopAssist uses. The free daily OpenRouter allowance is shared by everything on your account, including the live ShopAssist demo.

The page and document limits are low on purpose, to protect the free daily AI requests once embeddings arrive in Milestone 2.

## Run the tests

```sh
docker compose run --rm --build tests
```

Tests use a separate database (`pinpage_test`), created automatically and emptied before every test. Test PDFs are made inside the tests, so no real documents live in the repo. GitHub Actions runs the same command on every push, using only the placeholder values in `.env.example`.

No test contacts R2 or OpenRouter. The tests service replaces the R2 values and the OpenRouter key from `.env` with placeholders (see `docker-compose.yml`). Every test stores files in memory with `FakeStorage` and gets fixed embeddings and replies from `FakeAI`, and any request that reaches botocore's or the openai client's network layer fails the test.

## Check the R2 connection (manual)

Once your real R2 values are in `.env`, run this by hand to prove they work:

```sh
docker compose run --rm --no-deps --build api python scripts/check_storage.py
```

It saves one small file under `healthcheck/` in the bucket, reads it back, downloads it through a signed link, deletes it and checks it is gone, printing PASS or FAIL for each step. It never prints keys, links or secrets. It is not part of the automated tests.

## Check the AI connection (manual)

Once your real OpenRouter key is in `.env`, run this by hand to prove it works:

```sh
docker compose run --rm --no-deps --build api python scripts/check_ai.py
```

It uses exactly 2 real requests from your free daily allowance: it embeds one sentence (checking the embedding has `EMBEDDING_DIMENSIONS` numbers) and asks the chat model for a one word reply. It prints PASS or FAIL for each step and never prints the key. It is not part of the automated tests.

## Rebuild a document's sections

Documents processed before a change to section detection keep their old sections. To rebuild them from the stored pages, without re-embedding:

```sh
docker compose exec api python -m app.cli.documents rebuild-sections <document_id>
```

It reads the PDF from storage only for its bookmarks, moves each chunk to its new section, and changes no chunk text or embedding. If the new sections would cut the text into different chunks, it refuses and changes nothing: upload that document again instead. It prints counts only, never document text.

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
- Every documents query joins on workspace membership, so a document you can't reach is never even loaded. Someone else's document, a missing one and a malformed id all get the same 404.
- A conversation is reachable only by the person who started it, and only while they can still reach its document: both are checked inside the query. Retrieval for a question is limited to that conversation's document, also inside the query.
- Excerpt text and section titles come from uploaded files, so they are untrusted: anything imitating the prompt's tags is neutralized before it reaches the chat model, and the prompt tells the model to treat excerpts as data, never as instructions.
- The R2 bucket is private. Original files are reached only through signed links, made after the membership check, for ready documents only.
- Storage keys are built only from UUIDs (`workspaces/{workspace_id}/documents/{document_id}.pdf`), never from a user's file name.
- Uploads are checked by their own bytes (`%PDF-`), never by file name or claimed type. The size is checked twice: the declared size before anything is read, then the real bytes as they arrive.
- The workspace row is locked while an upload checks the document limit, so two uploads at once can't both squeeze past it.
- A failed upload leaves nothing behind in storage. Deleting removes the file before the row, so a storage failure never leaves a file nobody can delete.
- Storage and AI errors reach callers as one safe message. Logs record ids, codes, HTTP statuses and error types only: never keys, links, secrets, document text or provider error text.
- AI requests are paced and get at most 2 attempts, so a busy model can't use up the daily allowance.
- Real secrets live only in `.env`, which is never committed.

## Licence

MIT. See [LICENSE](LICENSE).
