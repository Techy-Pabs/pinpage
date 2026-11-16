# Pinpage Milestone 5 Spec

Nov 16, 2026 · Abraham Bassey

## Goal and scope

Milestone 5 gives Pinpage a face. A person signs in, uploads a handbook, watches it get ready, and asks questions in a chat where **every citation is a clickable page pin**: clicking it opens the original PDF at that exact page, beside the answer. That moment, an answer you can check in one click, is what the whole design is built around.

**In scope**

- Small backend additions the frontend needs (no AI requests)
- Mockups of every screen, approved before any frontend code
- A React frontend in `web/`: sign in, document library, upload, the document workspace (sections, chat, PDF viewer), and usage
- Accessibility, security headers and a mobile layout

**Out of scope**

- Deployment of either part (Milestone 6)
- Organisation workspaces and sharing (Milestone 7)
- Highlighting the cited sentence inside the PDF (a later improvement; Milestone 5 jumps to the page)

## Stack

React with Vite and Tailwind CSS, as in ShopAssist, in a `web/` folder of the same repository. Two additions, each for one job:

| Package | Why |
| --- | --- |
| react-router | Separate addresses for the library and each document, so the back button and links work |
| react-pdf | Shows the original PDF in the page using Mozilla's pdf.js, so a citation can open its page. Permissive licence |

No state management library: plain React state and a small API client are enough for this size.

## Backend additions (slice 0, no AI requests)

1. **CORS.** `ALLOWED_ORIGINS` (comma separated). Locally `http://localhost:5173`; the Vercel address is added in Milestone 6. Only these origins may call the API from a browser.
2. **Usage.** `GET /usage` returns `{"questions_used_today", "questions_per_day", "resets_at"}` for the current user, from `question_usage`, so the chat can show "12 of 20 questions left today".
3. **A local only fake AI.** `AI_PROVIDER` is `openrouter` (default) or `fake`. `fake` uses the same FakeAI as the tests, with answers that cite the first excerpt, so the whole interface can be clicked through without spending free requests. The app **refuses to start** with `AI_PROVIDER=fake` unless `APP_ENV=development`, and `APP_ENV` defaults to `production`, so it can never be switched on by accident in a deployment. Uploads still use real storage; only embeddings and answers are faked, and fake embeddings are marked so a document embedded with them can't be asked with the real AI later (it must be retried).
4. **Section outline for the viewer.** `GET /documents/{id}/sections` already exists; no change. `GET /documents/{id}/file` already returns a signed link.

**R2 CORS rule (you do this in Cloudflare).** The PDF viewer downloads the file directly from R2 through the signed link, so the `pinpage-dev` bucket needs a CORS rule allowing `GET` from `http://localhost:5173`. Steps are given when slice 0 is merged.

## Screens

### 1. Sign in and register

One page with a short explanation of Pinpage on one side and the form on the other. Register and sign in are two tabs of the same form. Errors use the API's messages. A note says this is a portfolio demo and documents should not be confidential.

### 2. Library

- The user's documents as cards or rows: title, pages, size, uploaded date and a status: Processing (with a quiet animated state), Ready, or Failed with its friendly message.
- Upload by dragging a PDF onto the page or choosing a file. Shows the limits (20 MB, 100 pages, 10 documents) and the API's errors (not a PDF, too large, duplicate with a link to the existing document, limit reached).
- While any document is processing, the list checks its status every 3 seconds and stops when none are.
- Failed documents show **Retry** when the code allows it (interrupted, ai_unavailable), otherwise only **Delete**.
- Delete asks for confirmation and says it can't be undone.
- An empty library shows a short welcome and an upload area.

### 3. Document workspace (the main screen)

Three areas on a wide screen:

| Left | Centre | Right |
| --- | --- | --- |
| Sections outline (front matter hidden), each with a **Summarise** action; conversations for this document, and **New conversation** | The chat | The PDF viewer, opened at the cited page |

- **Citations are page pins**: a small label such as "p. 10 · 4.1 Annual leave" after each answer. Clicking one opens the viewer at that page; hovering or focusing shows the snippet.
- **Not found answers look different** from answered ones (for example a muted style and no pins), so a user never mistakes a refusal for an answer.
- **Waiting is designed for**: answers take 10 to 40 seconds on free models, so the pending answer shows what is happening ("Reading pages 10 to 11...", "Searching the handbook...") based on the question's mode where it can be known in advance, and the send button is disabled until the answer arrives.
- **Errors are specific**: 503 says try again in a moment and keeps the question in the box; 429 says the daily limit is reached and when it resets; 404 returns to the library.
- **Usage** shows near the input: "8 of 20 questions left today".
- **Asking about a page**: the viewer has an "Ask about this page" action that fills the input with "What does page N say?".
- The viewer has page up and down, a page number field, and zoom, and keeps its place while chatting. The signed link is fetched again when it expires.

On a narrow screen (phones), the three areas become tabs: **Chat**, **Sections**, **Page**. Clicking a pin switches to the Page tab.

### 4. Shared parts

- A top bar with the Pinpage name, the library link and sign out.
- When the token expires (401), the user returns to sign in and comes back to the same page afterwards.

## Design

The design is done by hand in **Figma**, never generated, in three steps:

1. **Research.** Study real products that solve the same problems (reading long documents, answers with sources, a document beside a chat) and collect screenshots of what works into a Figma reference board, with a note on each saying which pattern it shows. Patterns are borrowed; logos, illustrations and exact visual styles are not copied.
2. **Direction.** From the board, choose the type, colours, spacing and the look of the page pin, as a small style sheet in the same Figma file.
3. **Mockups.** Every screen above, at desktop and phone width, approved before any frontend code. Claude Code builds from exported frames and the style sheet, matching them as closely as it can.

Principles: calm and trustworthy, like a well made reference book rather than a chat toy; type chosen for reading; generous spacing; the page pin as the one memorable visual idea. No generic "AI gradient" look. Colours meet WCAG AA contrast, and status is never shown by colour alone.

## Security and accessibility

- `web/vercel.json` with security headers and a Content Security Policy, as in ShopAssist, allowing the API origin, the R2 origin for the PDF and the pdf.js worker, and nothing else.
- The token is kept the same way as in ShopAssist's frontend.
- Answers are shown as plain text, never as HTML, since they come from the model and documents.
- Every action works with the keyboard; focus is visible; the chat announces new answers to screen readers; axe reports no serious issues on any screen.

## Tests

- Backend (slice 0): CORS allows only the listed origins; `/usage` counts correctly and resets at midnight UTC; the app refuses to start with `AI_PROVIDER=fake` outside development; fake answers cite an excerpt; a document embedded with fake embeddings can't be asked with the real AI.
- Frontend: Vitest tests for the API client (error envelope handling, 401 redirect), citation pin labels, and the polling that stops when nothing is processing.
- Manual browser checks for each frontend slice, at desktop and phone width, with the fake AI.

## Build plan

| Step | What | AI requests |
| --- | --- | --- |
| Slice 0 | Backend additions (CORS, usage, fake AI for local development) | None |
| Design | Research board, style sheet and mockups of every screen in Figma, approved together | None |
| Slice 1 | `web/` scaffold, API client, sign in and register, top bar, routing | None (fake AI) |
| Slice 2 | Library: list, upload, status polling, retry, delete | None (fake AI) |
| Slice 3 | Workspace: sections, conversations, chat, pins, usage, errors | None (fake AI) |
| Slice 4 | PDF viewer, pins opening pages, "Ask about this page", mobile tabs | None (fake AI) |
| Slice 5 | Accessibility pass, security headers and CSP, README | None |
| Final check | The demo handbook end to end with the real AI | About 10 |

Frontend slices are built and checked **one at a time** in the browser; only the backend slice and the docs run combined. Every commit: no "Co-Authored-By: Claude" trailer, no Claude-Session line, no mention of Claude as an author or contributor, and never stage `.env`.

## Done when

- [ ] Every screen matches its approved Figma mockup at desktop and phone width
- [ ] Clicking a citation opens the PDF at that page, on both sizes
- [ ] The whole app can be used with the fake AI, and the app refuses to start with it in production
- [ ] axe reports no serious issues; everything works by keyboard
- [ ] The final check with the real AI passes on the demo handbook
- [ ] All backend and frontend tests pass locally and in CI
- [ ] No commit mentions Claude, and no secret is in the Git history
