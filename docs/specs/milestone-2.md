# Pinpage Milestone 2 Spec

Nov 8, 2026 · Abraham Bassey

## Goal and scope

Milestone 2 makes Pinpage answer questions. When a PDF finishes processing, Pinpage also finds its sections, splits its text into chunks that remember their pages and section, and stores an embedding for each chunk. A user can then open a conversation with one document, ask a question, and get an answer that cites the exact page and section it came from. If the document doesn't cover the question, Pinpage says so and does not guess.

**In scope**

- Section detection: from the PDF's own outline (bookmarks) when it has one, otherwise from numbered headings in the text
- Chunking with page and section labels, using LangChain's text splitter
- The AI layer copied from ShopAssist: OpenRouter client, fallback models, timeouts, plain text replies, embeddings
- Embeddings created during processing, batched and paced to respect the free limits
- A retry endpoint for documents that failed for reasons that are not the file's fault
- Conversations with one document, private to the user who started them
- Retrieval limited to that one document, with answers that must cite their sources
- A daily question limit per user
- A manual AI check script, run by hand, like the storage check

**Out of scope (later milestones)**

- Page questions ("what does page 12 say?") and section summaries (Milestone 3)
- Rewriting follow up questions before retrieval (Milestone 3)
- The full quality check, injection defence tests and processing time limit (Milestone 4)
- The frontend (Milestone 5)

## Reused from ShopAssist

Copied and adapted from `../shopassist`, with every name changed to Pinpage:

- The OpenRouter client, `AIServiceError`, the 45 second timeout, fallback models, and the plain text reply rules
- The embeddings code, batched requests, and pgvector `halfvec` with an HNSW cosine index
- The grounding prompt pattern, tag neutralization for untrusted text, and the classifier label safety net
- The fake AI client used in tests, so no test ever calls OpenRouter

**New dependency:** `langchain-text-splitters` only. It is a small package with one job: splitting text into chunks while keeping paragraphs together. The rest of LangChain is not added. Our own code keeps control of retrieval, ownership, prompts and the AI layer.

## Processing, extended

Processing now has three stages. The document stays `processing` until all three finish.

1. **Extract pages** (Milestone 1, unchanged).
2. **Find sections and build chunks** (no AI requests).
3. **Create embeddings** (AI requests, batched and paced).

### Section detection

1. If the PDF has an outline (bookmarks), use it: each entry gives a title, a level and a start page.
2. Otherwise, look for heading lines at the start of a line, such as `4.`, `4.2`, `Section 4`, `Chapter 3`, `Part II`, or short lines in capitals, and use their numbering to work out the level.
3. A section runs from its start to the start of the next section at the same or a higher level.
4. If no headings are found, the whole document is one section called by the document's title.

Section detection never fails a document. At worst, everything sits in one section.

### Chunking

- Split each section's text with `RecursiveCharacterTextSplitter`, about 1,000 characters per chunk with 150 characters of overlap.
- Each chunk records `page_start` and `page_end` (a chunk can cross a page break), its section, and its order in the document.
- The text sent for embedding starts with a label: `Document title > 4.2 Annual Leave`. The label helps retrieval; the stored chunk content stays as the original text.

### Embeddings

- Chunks are embedded in batches of `EMBEDDING_BATCH_SIZE` (default 64), with no more than `AI_REQUESTS_PER_MINUTE` (default 15) requests a minute, safely under OpenRouter's 20.
- Each request has at most 2 attempts in total. A busy free model must not use up the daily allowance on retries.
- If embeddings cannot be created, the document fails with `ai_unavailable`. Its pages, sections and chunks are kept, so a retry only redoes the embeddings.

### New failure code and retry

| error_code | When | Message shown to the user |
| --- | --- | --- |
| ai_unavailable | Embeddings could not be created | Pinpage couldn't prepare this document for questions right now. Try again later. |

`interrupted` now says: *Processing was interrupted. Try again.*

`POST /documents/{id}/retry` restarts processing for a document that failed with `interrupted` or `ai_unavailable`. It answers 409 `retry_not_allowed` for any other status or code, because the same file would fail the same way. It resumes from the first unfinished stage.

An unexpected database error during processing now ends as `interrupted`, not `unreadable` (carried over from Milestone 1).

## Data model additions

| Table | Columns | Notes |
| --- | --- | --- |
| document_sections | id, document_id, position, number, title, level, start_page, end_page | number can be empty (for example a capitals heading). Deleted with its document |
| document_chunks | id, document_id, section_id, position, page_start, page_end, content, embedding, created_at | embedding is halfvec, nullable until embedded. HNSW cosine index. Index on document_id. Deleted with its document |
| conversations | id, document_id, user_id, title, created_at, updated_at | Private to user_id. Deleted with its document |
| messages | id, conversation_id, role, content, outcome, citations, created_at | role is user or assistant. outcome is answered or not_found for assistant messages. citations is JSON. Deleted with its conversation |

**Access rule for conversations:** you can reach a conversation only if you started it **and** you can still reach its document (membership inside the query, as in Milestone 1). In Milestone 7, members of a shared workspace share documents but never each other's conversations.

## Asking a question

`POST /conversations/{id}/messages` with `{"content": "..."}`:

1. Check the daily question limit (`DAILY_QUESTIONS_PER_USER`, default 20, counted from the user's messages today, in UTC). Over the limit: 429 `daily_limit_reached`.
2. Questions longer than 2,000 characters: 422.
3. Embed the question (1 AI request).
4. Retrieve the top `RETRIEVAL_TOP_K` (default 6) chunks **from this document only**, through the membership query, keeping only those above `RETRIEVAL_MIN_SIMILARITY` (start with ShopAssist's tuned value).
5. If no chunk passes the cut off, answer with the fixed not found message **without calling the chat model**. This saves a request. outcome is not_found.
6. Otherwise, send the chat model the grounding prompt, the last 3 question and answer pairs from this conversation, and the chunks as numbered excerpts `[S1]`, `[S2]` and so on, each labelled with its pages and section. Excerpt text is untrusted and goes through tag neutralization.
7. The model must cite excerpts by their markers. The server keeps only markers that match a real excerpt and turns them into citations. **If the answer cites nothing valid, it is replaced by the not found message** and outcome is not_found. Every answer Pinpage gives must point to a page.
8. Save the question and the answer together, in one transaction, and return the assistant message.

If the AI is unavailable, answer 503 `ai_unavailable` and save nothing.

**The not found message:** *I couldn't find this in the document. Try rephrasing, or ask about a specific section.*

**A citation** contains: marker, page_start, page_end, section_number, section_title, and a snippet of up to 200 characters from the chunk.

## Endpoints

All require login. A document or conversation you cannot reach answers 404 (`document_not_found` or `conversation_not_found`), whether it is missing, malformed or someone else's.

| Method and path | What it does | Success | Main errors |
| --- | --- | --- | --- |
| POST /documents/{id}/retry | Restarts processing after interrupted or ai_unavailable | 202 with the document | 404, 409 retry_not_allowed |
| GET /documents/{id}/sections | The detected sections in order | 200 | 404, 409 document_not_ready |
| POST /documents/{id}/conversations | Starts a conversation with a ready document | 201 | 404, 409 document_not_ready |
| GET /documents/{id}/conversations | Your conversations with this document, newest first | 200 | 404 |
| GET /conversations/{id} | One conversation with its messages, oldest first | 200 | 404 |
| POST /conversations/{id}/messages | Asks a question | 201 with the assistant message | 404, 422, 429 daily_limit_reached, 503 ai_unavailable |
| DELETE /conversations/{id} | Deletes a conversation and its messages | 204 | 404 |

A conversation's title is the first 80 characters of its first question, or "New conversation" before that.

## Configuration additions

| Variable | Example in .env.example | Notes |
| --- | --- | --- |
| OPENROUTER_API_KEY | your-openrouter-key | You add the real key yourself |
| AI_CHAT_MODEL | nvidia/nemotron-3-ultra-550b-a55b:free | As in ShopAssist |
| AI_CHAT_FALLBACK_MODELS | (ShopAssist's fallbacks) | Comma separated |
| AI_EMBEDDING_MODEL | nvidia/nemotron-3-embed-1b:free | As in ShopAssist |
| EMBEDDING_DIMENSIONS | (ShopAssist's value) | Must match the model |
| EMBEDDING_BATCH_SIZE | 64 | |
| AI_REQUESTS_PER_MINUTE | 15 | Under OpenRouter's 20 |
| CHUNK_SIZE | 1000 | Characters |
| CHUNK_OVERLAP | 150 | Characters |
| RETRIEVAL_TOP_K | 6 | |
| RETRIEVAL_MIN_SIMILARITY | (ShopAssist's value) | Tuned properly in Milestone 4 |
| DAILY_QUESTIONS_PER_USER | 20 | |

The free daily allowance is shared by everything on your OpenRouter account, including the live ShopAssist demo.

## Tests

No test calls OpenRouter or R2. The fake AI returns fixed embeddings and replies.

**Processing**

- A PDF with an outline gets sections from the outline; one without gets them from numbered headings; one with neither gets a single section.
- Chunks carry the right pages and section, including a chunk that crosses a page break.
- Embeddings are requested in batches, and a failure ends as ai_unavailable with pages, sections and chunks kept.
- Retry works after interrupted and ai_unavailable, resumes from the unfinished stage, and is refused for other failures and for ready documents.

**Conversations and ownership**

- User B gets 404 on A's conversations for every endpoint, and cannot start a conversation with A's document.
- Deleting a document deletes its conversations.

**Answers**

- Retrieval only ever returns chunks from the conversation's document, even when another document (same user or another user) is a better match.
- Below the cut off, the not found answer is returned and the chat model is not called.
- Valid markers become citations with the right pages and section; invalid markers are dropped; an answer with no valid citation becomes not found.
- Excerpt text containing tags or instructions is neutralized before it reaches the prompt.
- The daily limit, the 2,000 character limit, and 503 with nothing saved when the AI fails.

## Manual AI check

`scripts/check_ai.py` uses 2 real requests: it embeds one sentence (checking the vector has the expected dimensions) and asks the chat model for a one word reply. It prints PASS or FAIL and never prints the key. You run it by hand once.

## Build plan

Two combined runs, then your manual checks.

| Run | Branch | What it builds | Real AI requests |
| --- | --- | --- | --- |
| A | feature/processing-ai | Sections, chunks, the AI layer, embeddings in processing, retry, the check script | None in tests |
| B | feature/conversations | Conversations, retrieval, answers with citations, daily limit | None in tests |
| Manual | | You run check_ai.py, then upload a demo handbook and ask real questions | About 10 to 15 |

Separate commits per slice, tests after each slice, one review stop per run. Every commit: no "Co-Authored-By: Claude" trailer, no Claude-Session line, no mention of Claude as an author or contributor, and never stage `.env`.

## Done when

- [ ] All tests pass locally and in GitHub Actions
- [ ] check_ai.py passes against your real OpenRouter account
- [ ] A demo handbook uploads and reaches ready, with sensible sections
- [ ] A question the handbook answers gets a correct answer with the right page
- [ ] A question it doesn't answer gets the not found message
- [ ] A second account cannot reach the first account's conversations
- [ ] No commit mentions Claude, and no secret is in the Git history

## Decisions from slice reviews

Where this section disagrees with anything above, this section wins.

**Run A**

1. `langchain-text-splitters` cannot be installed alone: it requires `langchain-core`, which brings `langsmith`, `langchain-protocol` and some small libraries with it. They are accepted as dependencies of the splitter. Only `requirements.txt`'s own line is pinned, our code imports nothing but `RecursiveCharacterTextSplitter`, and LangSmith tracing is never configured.
2. The AI layer uses the versions ShopAssist runs with: `openai==1.109.1` and `pgvector==0.4.1`, not the newest releases. Upgrading them is a separate change.
3. Stages 1 and 2 (pages, sections and chunks) are saved in one transaction. A retry therefore resumes either at stage 1 (nothing saved) or at stage 3 (only the chunks still missing an embedding).
4. A retry checks the document limit like an upload, since a retried document counts toward it again (403 `document_limit_reached`).
5. The stuck document check counts from `updated_at`, which an upload or a retry sets, and keeps the stages a stuck document finished.
6. `OPENROUTER_API_KEY` is optional locally (documents fail with `ai_unavailable` without it) but must be set before deployment.
7. The request pacer is per process, so production runs a single worker until the pacer is shared (for example through the database).
8. Known section detection limits may be revisited in Milestone 3: a numbered heading whose title ends in a number ("3. Phase 2") is skipped as a contents page line, and a short capitalised list item without a full stop can be taken for a heading.
9. LangSmith tracing must never be enabled. The app refuses to start if `LANGCHAIN_TRACING_V2` or `LANGSMITH_TRACING` (or the variants `LANGSMITH_TRACING_V2` and `LANGCHAIN_TRACING`, which LangSmith also reads) is set to true.
