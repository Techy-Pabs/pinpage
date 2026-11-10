# Pinpage Milestone 3 Spec

Nov 9, 2026 · Abraham Bassey

## Goal and scope

Milestone 3 is what makes Pinpage more than a chatbot over PDFs. A user can ask about a page ("what does page 12 say?"), ask for a section summary ("summarise section 4"), and ask natural follow up questions ("and for part time staff?"). Page and section requests read the whole page or section instead of the few most similar chunks, so nothing in that part of the document is missed.

**In scope**

- Fixing section boundaries from bookmarks, and marking the front matter
- Recognising page and section requests in a question, without spending an AI request
- Page questions: up to 3 pages per question
- Section summaries, including sections too long for one request
- Rewriting short follow up questions so retrieval understands them
- A `mode` on every answer, so the frontend can show what kind of answer it is

**Out of scope (later milestones)**

- The quality check, processing time limit and further injection hardening (Milestone 4)
- The frontend, including a "Summarise" button on each section (Milestone 5)
- Questions across two documents

## 1. Section fixes (carried over from the Milestone 2 manual check)

**End pages from bookmarks.** A section that ends where the next section at the same or higher level starts at the **top of a page** ends on the page before. This rule already applies to headings found in the text; it must also apply to sections from the PDF's outline. In the Lantern Hill handbook, section 1 must be pages 4 to 5, not 4 to 6, and 4.8 must end on page 11, not 12.

How to tell "top of a page" for an outline entry: the heading's text appears within the first 3 non empty lines of that page, ignoring lines detected as running headers. If the heading text cannot be found on that page, keep the current behaviour (end on the start page of the next section).

**Front matter.** When the first detected section starts after page 1, the pages before it become one section titled "Front matter" with `kind = front_matter`. Every other section has `kind = body`. Front matter chunks are **excluded from retrieval and summaries**, because a contents page matches many questions weakly and would produce misleading citations. Page questions can still read front matter pages.

`document_sections` gains `kind` (text, check constraint: body or front_matter, default body), with a migration that sets existing rows to body.

**Reprocessing existing documents.** Existing documents keep their old sections. Add a management command, `python -m app.cli.documents rebuild-sections <document_id>`, that rebuilds sections and reassigns chunks to sections from the stored pages, **without re-embedding** (chunk text and embeddings do not change; only section_id and kind). If rebuilding would change chunk boundaries, the command refuses and says the document must be uploaded again.

## 2. Recognising the request (no AI request)

Before retrieval, a small parser reads the question and decides its **mode**:

| Mode | Recognised when the question contains | Examples |
| --- | --- | --- |
| page | "page N", "pages N to M", "pages N-M", "pages N and M", "p. N" | What does page 12 say? Summarise pages 10 to 11 |
| section | "section N", "section N.N", "chapter N", "part N" with a number that matches a detected section | Summarise section 4. What does section 4.6 say? |
| section | a summary verb ("summarise", "summarize", "summary of", "overview of"; "explain" was dropped, see decision 7) plus a title that matches a section | Summarise the leave policy. Give me an overview of probation |
| question | anything else | How long is paternity leave? |

**Title matching:** a section matches when every meaningful word of its title (ignoring "the", "and", "of", "policy", "section") appears in the question, case insensitive, ignoring simple plural s. If several sections match, choose the one with the longest title; if still tied, choose the higher level (4 before 4.1).

**Out of range or unknown:**

- A page number above the page count: answer without an AI request: *This document has N pages. Ask about a page between 1 and N.* (`outcome = not_found`, `mode = page`).
- A section number that doesn't exist: *I couldn't find section X in this document. The sections are listed under Sections.* (`outcome = not_found`, `mode = section`).
- More than 3 pages in one question: use the first 3 and say so at the end of the answer: *I looked at pages A to C only. Ask about later pages separately.*

**Page numbers** are the PDF's own page positions (page 1 is the first page of the file), which is what a PDF viewer shows. This is stated in the API docs.

The parser is a pure function with no database or AI access, so it is fully unit tested.

## 3. Page questions

- Load the requested pages' text from `document_pages`, in order, through the same membership query as everything else.
- Send them as excerpts `[S1]`, `[S2]`, one per page, labelled with the page number and the section that covers most of that page.
- Use a page prompt: the same rules as the grounding prompt, plus: *If the question only asks what the page says, give a short summary of the page in up to 5 lines.*
- Citations point to the page (page_start = page_end = that page) and its section.
- A page with no text (fewer than 20 characters) is answered without an AI request: *Page N has no readable text.*

## 4. Section summaries

- Load the section's chunks **including its subsections**, in document order, excluding front matter.
- Remove the overlap between consecutive chunks before joining, so the model doesn't read repeated text.
- Send the chunks as excerpts `[S1]`, `[S2]`... with their pages and subsection.
- **Short sections** (total text up to `SUMMARY_PART_CHARS`, default 12,000 characters): one request with the summary prompt.
- **Long sections:** split the excerpts into parts of up to `SUMMARY_PART_CHARS` each, summarise each part (keeping its markers), then one final request combines the part summaries. At most `SUMMARY_MAX_PARTS` (default 4) parts; above that, answer without an AI request: *Section X is too long to summarise in one go. Ask about one of its subsections: X.1, X.2...*
- **Summary prompt rules:** summarise only from the excerpts, up to 8 short lines, one point per line, every line ending with its markers, start with one sentence saying what the section covers, plain text only. The excerpt and injection rules from the grounding prompt apply unchanged.
- Markers are validated by the server exactly as for questions; a summary with no valid marker becomes the not found answer.
- A summary counts as **one question** toward the daily limit, however many requests it uses. This is why `SUMMARY_MAX_PARTS` is capped.

## 5. Follow up questions

A question is rewritten before retrieval **only when** the conversation already has at least one answered question **and** the question is short (12 words or fewer) or contains a reference word: it, its, they, them, this, that, these, those, there, he, she, what about, how about, and for, also.

- One chat request turns the question into a standalone question, using the last 3 question and answer pairs (without markers). Prompt: *Rewrite the latest question so it can be understood on its own, using the conversation only to fill in what it refers to. Return only the rewritten question.*
- The **rewritten question is used for retrieval only.** The answer prompt still receives the user's original question, plus the history, as now.
- The rewrite is sanity checked: at most 300 characters, one line, not empty. Otherwise the original question is used.
- If the rewrite request fails, fall back to the original question; never return 503 for a failed rewrite.
- The rewritten question is stored on the assistant message as `retrieval_query` (nullable), visible in the API for debugging, so we can see why an answer was found or missed.
- Page and section requests are parsed from the original question first; if the original has no page or section reference but the rewritten one does (for example "and section 5?" after a section summary), the rewritten one is parsed too.

## Data model changes

| Table | Change |
| --- | --- |
| document_sections | add `kind` (body or front_matter) |
| messages | add `mode` (question, page or section; nullable for user messages, required for assistant messages) and `retrieval_query` (nullable text) |

Existing assistant messages get `mode = question`.

## API changes

No new endpoints. Assistant messages in responses gain `mode` and `retrieval_query`. Sections in `GET /documents/{id}/sections` gain `kind`.

## Configuration additions

| Variable | Default | Notes |
| --- | --- | --- |
| MAX_PAGES_PER_QUESTION | 3 | |
| SUMMARY_PART_CHARS | 12000 | Characters per summary part |
| SUMMARY_MAX_PARTS | 4 | Caps the requests one summary can use at 5 |
| REWRITE_FOLLOW_UPS | true | Turns follow up rewriting off without code changes |

## Tests

No test calls OpenRouter or R2.

**Sections:** outline sections end on the page before when the next starts at the top of a page; a heading found mid page keeps the same page; front matter is created only when the first section starts after page 1; a document with no headings has no front matter; the rebuild command fixes an existing document without re-embedding and refuses when chunk boundaries would change.

**Parser:** every row of the table above, including plurals, "p. 5", ranges, reversed ranges ("pages 12 to 10"), out of range pages, unknown sections, ties in title matching, and ordinary questions that contain numbers but no page or section ("How many days after 5 years?").

**Page answers:** the right pages are sent in order; citations point to those pages; more than 3 pages uses the first 3 and adds the note; an empty page and an out of range page cost no AI request; another user's document still gives 404.

**Summaries:** subsections are included in order; front matter is excluded; overlap is removed; a short section uses one request; a long section uses one request per part plus one; too many parts costs no request; invalid markers are dropped; a summary counts once toward the daily limit.

**Follow ups:** rewriting happens only when the conditions hold; retrieval uses the rewritten question while the answer prompt gets the original; a failed or oversized rewrite falls back to the original without a 503; `REWRITE_FOLLOW_UPS=false` turns it off; `retrieval_query` is saved.

## Build plan

One combined run on `feature/pages-and-sections`, then the manual check.

| Slice | What it builds | Commit message |
| --- | --- | --- |
| 1 | Section end page fix, front matter, kind column, rebuild command | Fix bookmark section ends and mark front matter |
| 2 | The request parser and the mode and retrieval_query columns | Recognise page and section requests |
| 3 | Page answers and section summaries, with their prompts | Answer page questions and summarise sections |
| 4 | Follow up rewriting | Rewrite follow up questions for retrieval |

Tests after each slice, one review stop at the end. Every commit: no "Co-Authored-By: Claude" trailer, no Claude-Session line, no mention of Claude as an author or contributor, and never stage `.env`.

## Manual check (about 20 requests)

Run the rebuild command on the Lantern Hill handbook, then in one conversation:

| # | Ask | Expect |
| --- | --- | --- |
| 1 | What does page 11 say? | A short summary of maternity, paternity, compassionate leave and public holidays, all cited to page 11 |
| 2 | Summarise section 4 | Up to 8 lines covering annual, sick, maternity, paternity and special leave, cited to pages 10 and 11 |
| 3 | And section 5? | A summary of recruitment and induction (rewritten follow up), cited to page 12 or 13 |
| 4 | Give me an overview of probation | Section 5.4 summary |
| 5 | What does page 40 say? | The "This document has 20 pages" answer, no AI request |
| 6 | Summarise section 15 | The "couldn't find section 15" answer, no AI request |
| 7 | How long is paternity leave? Then: what about maternity? | 10 working days, then 16 weeks, both cited |
| 8 | GET /documents/{id}/sections | Section 1 is pages 4 to 5; the first row is Front matter |

## Done when

- [ ] All tests pass locally and in GitHub Actions
- [ ] The rebuild command fixes the handbook's sections without re-embedding
- [ ] Every row of the manual check behaves as expected
- [ ] No commit mentions Claude, and no secret is in the Git history

## Decisions from slice reviews

Where this section disagrees with anything above, this section wins.

1. Long sections are summarised part by part, then joined by a combine prompt: it uses only the part summaries, keeps their markers exactly, and follows the same 8 line rules. The server validates the final markers as for any answer.
2. The 3 line top of page rule applies to bookmark sections only. Headings found in the text keep the earlier rule (nothing but whitespace before the heading on its page).
3. Front matter also applies to documents without bookmarks: whenever the first section starts after page 1.
4. Any out of range page makes the whole answer the out of range message, even if other pages in the question exist.
5. Page and section answers skip the embedding request: they read pages or sections directly, without retrieval.
6. Part summaries get the question only, not the conversation history; the single summary request and the combine request get the history as well.
7. "explain" is not a summary verb, so "Explain how sick pay works" is answered as an ordinary question. The summary verbs are summarise, summarize, summary of and overview of.
8. `rebuild-sections` reads the PDF from storage for its outline, because bookmarks are not stored; the pages still come from the database. If storage is unavailable, it refuses and changes nothing.
9. In Milestone 4, add an overall time limit per answer, so a long summary (up to 5 requests, each with up to 2 attempts) cannot hold a request open for minutes.
