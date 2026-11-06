# Pinpage Milestone 1 Spec

Nov 5, 2026 · Abraham Bassey

## Goal and scope

Milestone 1 delivers the foundation of Pinpage: a person can register, upload a PDF, watch it move from processing to ready, see its text page by page, open the original file, and delete it, and nobody else can ever reach it.

There is no AI in this milestone. Chunking, embeddings and chat come in Milestone 2, so this milestone uses none of the 50 free OpenRouter requests a day.

**In scope**

- Project foundation copied from ShopAssist: auth, config, Docker Compose, Alembic, Pytest, CI
- Personal workspaces, created automatically at registration
- PDF upload to Cloudflare R2, with checks and limits
- Background extraction of text page by page, with a status and clear failure reasons
- Document library: list, view details, read a page, open the original through a signed link, delete
- Tests that try to reach another person's documents

**Out of scope (later milestones)**

- Section detection, chunking, embeddings and chat (Milestone 2)
- Page and section questions and summaries (Milestone 3)
- Organisation workspaces with shared members (Milestone 7)
- DOCX files, scanned PDFs and OCR
- The frontend (Milestone 5)

## Reused from ShopAssist, and what is new

Most of the foundation is copied from ShopAssist and adapted, not rewritten. Claude Code reads it from the `shopassist` folder next to `pinpage`.

**Copied and adapted**

- Auth: registration, login, JWT, Argon2 hashing, the same 401 for every auth failure, timing attack protection, admin role checked from the database
- Settings loaded from environment variables, with the app refusing to start when a required one is missing
- Docker Compose with the local pgvector image, the multi stage Dockerfile, and the start script (migrate, then serve)
- Alembic setup, the database session pattern, and the Pytest setup with a separate test database
- The GitHub Actions workflow that runs the tests on every push
- The PDF text extraction library ShopAssist already uses

**Waiting for Milestone 2:** the AI layer, the embeddings code and the RAG pipeline. They are not copied yet, so nothing unused sits in the code.

**New dependencies**

| Package | Why Pinpage needs it |
| --- | --- |
| boto3 | R2 speaks the same language as Amazon S3, and boto3 is the standard Python library for it: uploads, downloads, deletes and signed links |
| pypdf | Reads PDFs page by page. If ShopAssist already uses it, nothing changes. Avoid PyMuPDF: its AGPL licence can force client projects to publish their code |
| python-multipart | Lets FastAPI receive file uploads. ShopAssist probably has it already |

LangChain is not added in this milestone. Its text splitter arrives in Milestone 2, where chunking happens.

## Data model

Documents belong to a **workspace**, not directly to a user. In Milestone 1 every user has exactly one personal workspace, created at registration. In Milestone 7, organisation workspaces add more members to a workspace, and the access rule stays the same: you can reach a document only if you are a member of its workspace.

All ids are UUIDs, so they cannot be guessed by counting.

| Table | Columns | Notes |
| --- | --- | --- |
| users | id, email (unique), password_hash, is_admin, created_at | Adapted from ShopAssist, but differs from it: UUID id (not an integer), password_hash (not hashed_password), is_admin (not a role column) |
| workspaces | id, name, kind, created_at | kind is personal for now; organisation arrives in Milestone 7 |
| workspace_members | workspace_id, user_id, role, created_at | Primary key is the pair (workspace_id, user_id). role is owner for now |
| documents | id, workspace_id, uploaded_by, title, original_filename, storage_key, size_bytes, sha256, page_count, status, error_code, error_message, created_at, updated_at, processed_at | status is processing, ready or failed. Unique on (workspace_id, sha256) |
| document_pages | id, document_id, page_number, text, char_count | Unique on (document_id, page_number). Deleted with its document |

**Rules**

- Registration creates the user, the personal workspace and the owner membership in **one transaction**. If any part fails, none of it is saved.
- page_count is empty until processing finishes.
- error_code is a short fixed code (see the next section); error_message is a friendly sentence written by us, never a raw exception.
- Deleting a document deletes its pages through the database (ON DELETE CASCADE).
- Section tables and chunk tables arrive in Milestone 2.

## Upload and background processing

An upload answers within a second or two with status processing; the slow work happens afterwards, in a FastAPI background task.

**During the upload request**

1. Check the user is logged in.
2. Check the workspace has fewer than 10 documents. Otherwise refuse with document_limit_reached.
3. Read the file, stopping once it passes 20 MB, so a huge upload cannot fill the server's memory.
4. Check the first bytes are `%PDF`. The file name and the type the browser claims are not trusted.
5. Compute the SHA 256 fingerprint. If the same file already exists in this workspace, refuse with duplicate_document and return the existing document's id.
6. Save the file to R2, then create the document row with status processing.
7. Schedule the background task and answer 202 Accepted with the document.

**In the background task**

1. Download the file from R2 by its storage key. Reading from storage, not from memory, means a proper job queue can replace the background task later without changing this code.
2. Open it with pypdf and check the page count is 100 or fewer.
3. Extract the text of each page and save one document_pages row per page.
4. If at least 80% of pages have fewer than 20 characters, treat it as a scan and fail with no_text_layer.
5. Set status ready, page_count and processed_at. On any failure, set status failed with the matching code and message, and delete any pages already saved.

**Failure codes**

| error_code | When | Message shown to the user |
| --- | --- | --- |
| too_many_pages | More than 100 pages | This document has more than 100 pages. Upload a shorter file. |
| encrypted | The PDF is password protected | This PDF is password protected. Remove the password and upload it again. |
| no_text_layer | Most pages have no text | This looks like a scanned document. Pinpage can't read scans yet. |
| unreadable | pypdf cannot open the file | We couldn't read this PDF. It may be damaged. |
| interrupted | The server restarted during processing | Processing was interrupted. Delete this document and upload it again. |

**Stuck documents.** If the server restarts mid task, a document could stay processing forever. When the app starts, any document still processing after 15 minutes becomes failed with interrupted.

**Limits** (all set in config, so they can change without code changes): 20 MB per file, 100 pages per document, 10 documents per workspace. The page and document limits are low on purpose, to protect the free daily AI requests once embeddings arrive in Milestone 2.

## File storage in R2

Original PDFs live in the private `pinpage-dev` bucket and are reached only through short lived signed links, handed out after the ownership check.

**A storage layer with two versions**, the same pattern as ShopAssist's AI layer:

- `R2Storage` talks to Cloudflare through boto3.
- `FakeStorage` keeps files in memory for tests, so tests never touch the real bucket.
- Both offer the same four actions: save, read, delete, and create a signed link.

**Storage keys** follow `workspaces/{workspace_id}/documents/{document_id}.pdf`. The user's own file name never goes into the key, so strange or malicious names cannot cause problems in storage.

**Signed links** last 5 minutes and are created only after the API has checked that the user is a member of the document's workspace. The bucket itself stays private.

**Deleting** removes the file from R2 first, then the database row (its pages go with it). If R2 fails, the API answers 502 and keeps the row so the user can try again. A file already missing from R2 counts as deleted.

**A manual check script**, `scripts/check_storage.py`, saves, reads, links and deletes one small test file in the real bucket. You run it by hand once to prove the keys work. It is never part of the automated tests.

## Endpoints

Every document endpoint requires login and answers **404 document_not_found** both when a document does not exist and when it belongs to someone else. Answering 403 would confirm that the document exists.

| Method and path | What it does | Success | Main errors |
| --- | --- | --- | --- |
| POST /auth/register | Creates the user, personal workspace and owner membership | 201 | 409 email taken, 422 invalid input |
| POST /auth/login | Returns a JWT | 200 | 401 |
| GET /auth/me | Returns the current user | 200 | 401 |
| POST /documents | Uploads a PDF (multipart form: file, optional title) | 202 with the document, status processing | 400 not_pdf, 413 file_too_large, 409 duplicate_document, 403 document_limit_reached |
| GET /documents | Lists the user's documents, newest first | 200 | 401 |
| GET /documents/{id} | One document with its status; the frontend checks this until it is ready or failed | 200 | 404 |
| GET /documents/{id}/pages/{page_number} | The extracted text of one page | 200 | 404, 409 document_not_ready |
| GET /documents/{id}/file | A signed link to the original, valid 5 minutes | 200 with url and expires_in | 404 |
| DELETE /documents/{id} | Deletes the file, the document and its pages | 204 | 404, 502 storage_error |

**A document in responses** contains: id, title, original_filename, size_bytes, page_count, status, error_code, error_message, created_at, processed_at. It never contains storage_key, sha256 or workspace ids.

**Errors** use one shape everywhere: `{"error": {"code": "...", "message": "..."}}`.

**Title** defaults to the file name without `.pdf`, trimmed to 200 characters.

## Configuration

Real values live only in `.env` on your machine (and later in Render's settings). The repo holds `.env.example` with placeholders, and `.env` is in `.gitignore` from the very first commit.

| Variable | Example in .env.example | Notes |
| --- | --- | --- |
| DATABASE_URL | postgresql://pinpage:pinpage@db:5432/pinpage | Local Docker database |
| JWT_SECRET | change-me | Generate a long random value for real use |
| JWT_EXPIRE_MINUTES | 30 | As in ShopAssist |
| R2_ACCOUNT_ID | your-account-id | Used to build the endpoint address |
| R2_ACCESS_KEY_ID | your-access-key-id | From the token you created |
| R2_SECRET_ACCESS_KEY | your-secret-access-key | From the token you created |
| R2_BUCKET | pinpage-dev | |
| MAX_UPLOAD_MB | 20 | |
| MAX_PAGES | 100 | |
| MAX_DOCUMENTS_PER_WORKSPACE | 10 | |
| SIGNED_URL_SECONDS | 300 | |
| PROCESSING_TIMEOUT_MINUTES | 15 | Used by the stuck document check |

The app refuses to start if a required value is missing, except in tests, which use FakeStorage. You copy your real R2 keys into `.env` yourself. Claude Code never sees them, and they are never pasted into a prompt or this chat.

**Claude Code attribution.** The first commit includes `.claude/settings.json` with attribution turned off, as in the portfolio repo.

## Tests

The most important tests in this milestone are the ones that try to break ownership. Test PDFs are generated inside the tests (a text PDF, an image only PDF, an encrypted PDF, a 101 page PDF, a damaged file), so no real documents are stored in the repo.

**Ownership (two users, A and B, each with a document)**

- B gets 404 on A's document for: details, a page, the signed link, and delete.
- After B's delete attempt, A's document and its file still exist.
- B's list never contains A's document.
- The same PDF uploaded by A and by B is accepted for both, because they are in different workspaces.
- Every documents query in the code filters by workspace membership inside the query. A test checks a request with a valid token for a user with no membership gets nothing back.

**Registration**

- Registering creates exactly one personal workspace with the user as owner.
- If creating the workspace fails, the user is not saved either.
- The copied ShopAssist auth tests all pass.

**Upload and processing**

- A good PDF returns 202 and processing, then becomes ready with the right page count and one page row per page.
- A text file renamed to .pdf is refused with not_pdf.
- A file over the limit is refused with file_too_large.
- The same file twice in one workspace is refused with duplicate_document.
- The eleventh document is refused with document_limit_reached.
- The 101 page, encrypted, image only and damaged files end as failed with the right code, and no pages are kept.
- A document stuck in processing past the timeout becomes failed with interrupted at startup.
- Error messages never contain exception text or file paths.

**Library and storage**

- Reading a page of a document still processing returns document_not_ready.
- The signed link is created only for the owner; the storage key never appears in any response.
- Deleting removes the file from storage and all page rows.
- If storage deletion fails, the API answers 502 and the document is kept.

No automated test calls the real R2 or any AI service.

## Build slices

Seven slices, each on its own branch, each following the same loop: Claude Code builds it, you read every change, the tests pass, you explain it back and direct one small change yourself, then commit and merge. I'll write the exact prompt for each slice when you reach it.

| Slice | Branch | What it builds |
| --- | --- | --- |
| 0 | setup/foundation | Copy and adapt ShopAssist's foundation: auth, settings, Docker Compose, Dockerfile, Alembic, Pytest, GitHub Actions, .gitignore, .env.example, .claude/settings.json, a short README |
| 1 | feature/workspaces | workspaces and workspace_members tables, the migration, registration in one transaction, the tests |
| 2 | feature/storage | The storage layer: R2Storage, FakeStorage, settings, and the manual check script, which you run once against the real bucket |
| 3 | feature/upload | documents table and migration, POST /documents with every check and limit; processing left as a placeholder |
| 4 | feature/extraction | document_pages table, the background task, failure codes, and the stuck document check |
| 5 | feature/library | List, details, page, signed link and delete, plus the full ownership test sweep |
| 6 | docs/milestone-1 | README update, Swagger review, and a manual run through with a real PDF on the real bucket |

**Every commit prompt states:** no "Co-Authored-By: Claude" trailer, no Claude-Session line, and no mention of Claude as an author or contributor. Never stage `.env` or any real key.

## Done when

- [ ] `docker compose up` starts the API and the database, and migrations run automatically
- [ ] All tests pass locally and in GitHub Actions
- [ ] In Swagger, you register, upload a real 30 to 60 page PDF, watch it reach ready, read page 5, open the signed link, and delete it
- [ ] A second account cannot see, read, open or delete the first account's document, and you have tried it yourself in Swagger
- [ ] A scanned PDF ends as failed with the scan message
- [ ] The manual storage check script passes against `pinpage-dev`
- [ ] No `.env` or real key appears anywhere in the Git history
- [ ] No commit mentions Claude as an author or contributor
- [ ] You can explain the background task, signed links, and why the ownership filter sits inside the query

## Decisions from slice reviews

Made on Nov 6, 2026, after reviewing this spec against ShopAssist. Where this section disagrees with anything above, this section wins.

**Foundation (Slice 0)**

1. The users table follows this spec, not ShopAssist: UUID `id`, `password_hash`, `is_admin`. The JWT `sub` holds the user's UUID as a string.
2. The secret is named `JWT_SECRET` (ShopAssist used `JWT_SECRET_KEY`). `JWT_EXPIRE_MINUTES` is 30, as in ShopAssist; the 60 in the Configuration table was wrong.
3. Registering an email that is already taken returns 409. Every error, from the start, uses the envelope `{"error": {"code": "...", "message": "..."}}`. The 401 stays identical for every auth failure.
4. `.env.example` also holds `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`, which the local database container needs.
5. `require_admin` (reading `is_admin` from the database on every request) and the promote/demote command are kept, with small admin tests.
6. CORS is left out until Milestone 5, when the frontend arrives.
7. boto3, pypdf and python-multipart are added, and the R2 and limit settings are read, only in the slices that use them. They are placeholders in `.env.example` from Slice 0.
8. ShopAssist had no GitHub Actions workflow, so a new one is written. It uses only `.env.example`, never real secrets.
9. `.claude/settings.json` is tracked, with attribution `commit` and `pr` both set to `""`. `.claude/settings.local.json` is in `.gitignore`.
10. The MIT licence is copied, with copyright holder Abraham Bassey and year 2026.

**Later slices**

11. Failed documents do not count toward the limit of 10 documents per workspace.
12. Signed links are created only for ready documents; otherwise the API answers 409 `document_not_ready`.
13. A page number past the end of a document returns 404 `page_not_found`.
14. A failed document still blocks re-uploading the same file. `duplicate_document` returns the existing document's id and its status.
15. A PDF that pypdf can open with an empty password counts as readable, not encrypted.
16. Slice 4 checks for the newest pypdf release before pinning it.
17. Tests inject `FakeStorage` directly. There is no test mode switch in the settings.
18. The workspace row is locked during the document limit check, so two uploads at once cannot pass it together.
19. A failed upload must not leave an orphaned file in R2.
20. Background tasks open their own database session; they never reuse the request's.
21. When user deletion is added, it must delete the user's personal workspace explicitly, since the membership cascade does not remove it.
