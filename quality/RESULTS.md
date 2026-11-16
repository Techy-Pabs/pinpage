# Quality results

One line per quality run we decide to record. Full results of every run are saved in `quality/results/` (not committed).

| Date | Model | Cut off | Score | Notes |
| --- | --- | --- | --- | --- |
| 2026-11-16 | none (retrieval only) | cut off table | 18 of 18 answerable kept at 0.35, 17 of 18 at 0.40 | Chose 0.35 |
| 2026-11-16 | 8 case comparison, 4 models | - | see notes | nemotron-3-ultra 8 of 8 (median 10.6 s); nemotron-3.5-lightning 8 of 8 (median 11.8 s, one 82 s page answer after a timeout); nemotron-3.5-super-vl 2 of 8 (connection errors, not model failures); gemma-4-31b 0 of 8 (connection errors, then provider 429) |
