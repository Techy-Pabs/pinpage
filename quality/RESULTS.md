# Quality results

One line per quality run we decide to record. Full results of every run are saved in `quality/results/` (not committed).

| Date | Model | Cut off | Score | Notes |
| --- | --- | --- | --- | --- |
| 2026-11-16 | none (retrieval only) | cut off table | 18 of 18 answerable kept at 0.35, 17 of 18 at 0.40 | Chose 0.35 |
| 2026-11-16 | 8 case comparison, 4 models | - | see notes | nemotron-3-ultra 8 of 8 (median 10.6 s); nemotron-3.5-lightning 8 of 8 (median 11.8 s, one 82 s page answer after a timeout); nemotron-3.5-super-vl 2 of 8 (connection errors, not model failures); gemma-4-31b 0 of 8 (connection errors, then provider 429) |
| 2026-11-17 | nemotron-3.5-super-vl (retest, alone) | 0.35 | 6 of 6 | Retest after the connection errors of 2026-11-16; median 3.9 s |
| 2026-11-17 | nemotron-3-ultra, fallbacks lightning then super-vl | 0.35 | 25 of 25 | Full run with the chosen settings; every answer from ultra; median 4.4 s; one connection error, retried |

## Milestone 4 results (2026-11-17)

Two runs today, both with the cut off at 0.35, 6 excerpts per question and the old output limits (2500 question, 4000 page, 5000 summary, 1000 rewrite):

- **super-vl retest** (`20261117-061306`): `nvidia/nemotron-3.5-super-vl:free` alone, on the 6 cases it failed with connection errors on 2026-11-16. **6 of 6** passed, median 3.9 s. Those failures were the connection, not the model, so it stays as the last fallback.
- **Full run** (`20261117-061859`): the chosen models (`nvidia/nemotron-3-ultra-550b-a55b:free`, then `nvidia/nemotron-3.5-lightning:free`, then `nvidia/nemotron-3.5-super-vl:free`) on all 25 cases. **25 of 25** passed, median **4.4 s**, slowest 16.3 s (the section summary). Ultra answered every request; no fallback was used. Two cases (`gym`, `inject-leak`) need no chat request.
- **One connection error, retried.** It was seen in the logs during the full run, not in the result files: the retry succeeded, so every request in the files finished normally.

### Output limits

Largest output per request, in tokens (reasoning included), and every `finish_reason` seen:

| Kind | Requests (retest / full) | Largest, retest | Largest, full run | Largest seen | `finish_reason` |
| --- | --- | --- | --- | --- | --- |
| question | 4 / 22 | 371 | 322 | 371 | `stop` only |
| page | 1 / 1 | 636 | 377 | 636 | `stop` only |
| summary | 1 / 1 | 1096 | 782 | 1096 | `stop` only |
| rewrite | 0 / 1 | - | 68 | 68 | `stop` only |

Chosen limits:

| Setting | Was | Now | Times the largest seen |
| --- | --- | --- | --- |
| `AI_MAX_TOKENS_QUESTION` | 2500 | 2000 | 5.4 |
| `AI_MAX_TOKENS_PAGE` | 4000 | 2500 | 3.9 |
| `AI_MAX_TOKENS_SUMMARY` | 5000 | 3000 | 2.7 |
| `AI_MAX_TOKENS` (rewrites) | 1000 | 1000 (unchanged) | 14.7 |

Why not closer to the measurements:

- **Reasoning tokens count against the limit, and they vary.** They were most of every reply here (915 of 1096 for super-vl's summary), and they change between runs and between the fallback models: super-vl's replies were up to twice as long as ultra's for the same case.
- **Few samples.** There were only one or two requests each for page, summary and rewrite.
- **Ultra failed at 1000 before.** In the week before Run B it spent the whole 1,000 token allowance reasoning and returned an empty reply.

So each limit is at least 2.5 times the largest reply seen and at least 1000. They are a ceiling against runaway replies, so that one answer still fits within the 90 second answer deadline (`ANSWER_TIME_LIMIT_SECONDS`), not a target. A reply that reaches its limit is trimmed after its last valid marker, or gives 503 `ai_unavailable` if nothing is cited.
