# Agent efficiency: with and without Krusgraph

Measured, not claimed. Setup A has `list_tables` and `run_sql` (read-only T-SQL); setup B has the same two tools plus Krusgraph's MCP tools. Same model, system prompt (apart from the tool list), temperature and questions; the raw records are the JSONL files next to this report. Resource figures are over the **correct** answers only.

> **Mock run.** These records come from `python -m bench.mock_run`: a replaying fake model with constant, invented token counts, SQL on a SQLite copy of the seed rows, canned Krusgraph tools. It exercises the pipeline and the report; it measures nothing about any model or about Krusgraph. Real runs replace these files.

## Runs

| setup | runs | questions | correct | wrong | refused | error | share correct | model(s) | run id(s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 60 | 20 | 56 | 3 | 1 | 0 | 93 % | mock-model (replayed transcript, no API call) | 2026-10-26-mock-A |
| B | 60 | 20 | 59 | 1 | 0 | 0 | 98 % | mock-model (replayed transcript, no API call) | 2026-10-26-mock-B |

## Per correct answer (median, p25–p75, min–max)

| metric | A | B |
| --- | --- | --- |
| tokens (all) | 3740 (3740–5610, 3740–7480, n=56) | 17080 (12810–17080, 12810–21350, n=59) |
| input tokens | 1800 (1800–5400, 1800–7200, n=56) | 8400 (6300–16800, 6300–21000, n=59) |
| output tokens | 140 (140–210, 140–280, n=56) | 280 (210–280, 210–350, n=59) |
| tokens read from cache | 1800 (0–1800, 0–1800, n=56) | 6300 (0–6300, 0–8400, n=59) |
| tool calls | 2 (2–3, 2–4, n=56) | 4 (3–4, 3–5, n=59) |
| failed queries | 0 (0–0, 0–1, n=56) | 0 (0–0, 0–1, n=59) |
| SQL statements | 1 (1–2, 1–3, n=56) | 1 (1–2, 1–2, n=59) |
| GraphQL calls | 0 (0–0, 0–0, n=56) | 0 (0–0, 0–1, n=59) |
| Fabric CPU ms | – | – |
| Fabric data scanned MB | – | – |
| wall time s | 0 (0–0, 0–0.0, n=56) | 0 (0–0, 0–0.0, n=59) |

## B against A

Difference of per-question means (B − A) with a 95 % bootstrap interval over the questions (2000 resamples). Accuracy uses every judged run; the other rows use correct runs only, so a question with no correct run in one setup drops out of that row (the `questions` column says how many remain). An interval that contains 0 means the data does not separate the two setups on that metric.

| metric | questions | mean A | mean B | B − A | 95 % interval |
| --- | --- | --- | --- | --- | --- |
| share correct | 20 | 0.933 | 0.983 | +0.05 | -0.0167 to +0.117 |
| tokens (all) | 20 | 4.38e+03 | 1.53e+04 | +1.1e+04 | +1.02e+04 to +1.19e+04 |
| input tokens | 20 | 2.94e+03 | 1.06e+04 | +7.65e+03 | +7.08e+03 to +8.33e+03 |
| output tokens | 20 | 164 | 251 | +87.5 | +74.1 to +104 |
| tokens read from cache | 20 | 1.28e+03 | 4.5e+03 | +3.22e+03 | +2.96e+03 to +3.52e+03 |
| tool calls | 20 | 2.34 | 3.59 | +1.25 | +1.06 to +1.48 |
| failed queries | 20 | 0.05 | 0.1 | +0.05 | -0.05 to +0.183 |
| SQL statements | 20 | 1.34 | 1.34 | -3.33e-17 | -0.075 to +0.075 |
| GraphQL calls | 20 | 0 | 0.1 | +0.1 | +0 to +0.25 |
| Fabric CPU ms | 0 | – | – | – | not comparable |
| Fabric data scanned MB | 0 | – | – | – | not comparable |
| wall time s | 20 | 5e-05 | 3.33e-05 | -1.67e-05 | -5e-05 to +0 |

Runs per setup: A: 60, B: 60. Questions with Fabric CPU figures: A: 0, B: 0 (queryinsights data is attached by bench/fabric_metrics.py; in setup B Krusgraph's own statements are untagged and are reported separately by that script).

## Energy and CO₂ (optional, factor-based)

Region for the grid factor: `swedencentral`. Only factors with a source in bench/factors.yaml are used; everything else is "not estimated". Figures are per correct answer (median) and always an interval.

| setup | model energy Wh | Fabric CPU energy Wh | model CO₂ g | Fabric CO₂ g |
| --- | --- | --- | --- | --- |
| A | not estimated | not estimated | not estimated | not estimated |
| B | not estimated | not estimated | not estimated | not estimated |

Factors:
- `tokens.energy_wh_per_1000_tokens`: no source yet
- `fabric.cpu_hour_kwh`: no source yet
- `fabric.cu_hour_kwh`: no source yet
- `grid.swedencentral`: no source yet
- `grid.westeurope`: no source yet
- `grid.northeurope`: no source yet

## Limits

- The shop schema is a synthetic demo with seven small tables, not a customer's lakehouse; the questions were written by the people who built Krusgraph.
- Rebound: a tool that makes answers cheaper can lead to more questions being asked; this measures cost per answered question, not total use.
- Model version and date: the model id is in every record; a later model, prompt or Krusgraph version changes the numbers.
- Caching: prompt-cache reads are counted separately; a warm cache on repeat 2 and 3 lowers input tokens for both setups, and B's longer tool list caches differently from A's.
- Krusgraph's own server (Azure Container Apps) is not in these figures unless measured separately; in setup B its reads of the SQL endpoint show up in queryinsights without a bench tag.
- queryinsights attributes CPU time per statement with a delay of up to 15 minutes and omits system queries; columns the view does not have are reported as missing rather than guessed.
- Three repeats per question is enough to see large differences, not small ones; the bootstrap interval says how much the twenty questions support.
