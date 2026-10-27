# Agent efficiency: with and without Krusgraph

Measured, not claimed. Setup A has `list_tables` and `run_sql` (read-only T-SQL); setup B has the same two tools plus Krusgraph's read-only MCP tools; setup C the same two plus only the read-only Krusgraph tools that help answer a question (bench/mcp_tools.py, QA_TOOLS). Same model, settings (effort and max_tokens as recorded; no sampling parameters), system prompt and questions; the tool list is the only difference. `+cache` marks runs with prompt caching on. The raw records are the JSONL files next to this report. Resource figures are over the **correct** answers only.

> **Mock run.** These records come from `python -m bench.mock_run`: a replaying fake model with constant, invented token counts, SQL on a SQLite copy of the seed rows, canned Krusgraph tools. It exercises the pipeline and the report; it measures nothing about any model or about Krusgraph. Real runs replace these files.

## Runs

| setup | runs | questions | correct | wrong | refused | error | share correct | model(s) | run id(s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | 75 | 25 | 69 | 5 | 1 | 0 | 92 % | mock-model (replayed transcript, no API call) | 2026-10-26-mock-A |
| B | 75 | 25 | 74 | 1 | 0 | 0 | 99 % | mock-model (replayed transcript, no API call) | 2026-10-26-mock-B |

## By question tag

Share correct and the median tokens per correct answer, per tag (a question can carry more than one tag). `ship_to` and `join_path` need the link no name rule makes (orders.ship_to_id -> addresses); `composite` needs the line key of order_items.

| tag | A correct | A tokens | B correct | B tokens |
| --- | --- | --- | --- | --- |
| composite | 2/3 | 3740 | 3/3 | 12810 |
| hops:1 | 24/24 | 3740 | 24/24 | 12810 |
| hops:2-3 | 24/24 | 3740 | 23/24 | 12810 |
| join_path | 13/15 | 3740 | 15/15 | 21350 |
| ship_to | 19/24 | 3740 | 24/24 | 21350 |

## Per correct answer (median, p25–p75, min–max)

| metric | A | B |
| --- | --- | --- |
| tokens (all) | 3740 (3740–5610, 3740–7480, n=69) | 17080 (12810–21350, 12810–25620, n=74) |
| input tokens | 1800 (1800–5400, 1800–7200, n=69) | 8400 (6300–16800, 6300–25200, n=74) |
| output tokens | 140 (140–210, 140–280, n=69) | 280 (210–350, 210–420, n=74) |
| tokens read from cache | 1800 (0–1800, 0–1800, n=69) | 6300 (0–8400, 0–10500, n=74) |
| tokens written to cache | 0 (0–0, 0–0, n=69) | 0 (0–0, 0–0, n=74) |
| model cost USD (list price) | – | – |
| tool calls | 2 (2–3, 2–4, n=69) | 4 (3–5, 3–6, n=74) |
| failed queries | 0 (0–0, 0–1, n=69) | 0 (0–0, 0–1, n=74) |
| SQL statements | 1 (1–2, 1–3, n=69) | 1 (1–2, 1–2, n=74) |
| GraphQL calls | 0 (0–0, 0–0, n=69) | 0 (0–0, 0–1, n=74) |
| Fabric CPU ms | – | – |
| Fabric data scanned MB | – | – |
| wall time s | 0 (0–0, 0–0.0, n=69) | 0 (0–0, 0–0.0, n=74) |

## B against A

Difference of per-question means (B − A) with a 95 % bootstrap interval over the questions (2000 resamples). Accuracy uses every judged run; the other rows use correct runs only, so a question with no correct run in one setup drops out of that row (the `questions` column says how many remain). An interval that contains 0 means the data does not separate the two setups on that metric.

| metric | questions | mean A | mean B | B − A | 95 % interval |
| --- | --- | --- | --- | --- | --- |
| share correct | 25 | 0.92 | 0.987 | +0.0667 | +0.0133 to +0.133 |
| tokens (all) | 25 | 4.33e+03 | 1.68e+04 | +1.25e+04 | +1.12e+04 to +1.39e+04 |
| input tokens | 25 | 2.86e+03 | 1.16e+04 | +8.69e+03 | +7.77e+03 to +9.69e+03 |
| output tokens | 25 | 162 | 276 | +114 | +91.5 to +139 |
| tokens read from cache | 25 | 1.31e+03 | 5e+03 | +3.69e+03 | +3.28e+03 to +4.14e+03 |
| tokens written to cache | 25 | 0 | 0 | +0 | +0 to +0 |
| model cost USD (list price) | 0 | – | – | – | not comparable |
| tool calls | 25 | 2.31 | 3.94 | +1.63 | +1.31 to +1.99 |
| failed queries | 25 | 0.04 | 0.08 | +0.04 | -0.04 to +0.147 |
| SQL statements | 25 | 1.31 | 1.34 | +0.0267 | -0.04 to +0.1 |
| GraphQL calls | 25 | 0 | 0.08 | +0.08 | +0 to +0.2 |
| Fabric CPU ms | 0 | – | – | – | not comparable |
| Fabric data scanned MB | 0 | – | – | – | not comparable |
| wall time s | 25 | 4e-05 | 2.67e-05 | -1.33e-05 | -6.67e-05 to +4e-05 |

Runs per setup: A: 75, B: 75. Questions with Fabric CPU figures: A: 0, B: 0 (queryinsights data is attached by bench/fabric_metrics.py; in setups B and C Krusgraph's own statements are untagged and are reported separately by that script).

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
- Caching: without `--cache` every step pays the full input price for the tool list and the transcript; with it (the `+cache` setups) later steps read them from the cache. Each setup is compared with A at the same caching setting. Model cost is at list prices (bench/estimate.py: cache reads at the model's cache price, writes at 1.25 × input for the 5-minute TTL); token counts alone overstate what cached runs cost.
- Krusgraph's own server (Azure Container Apps) is not in these figures unless measured separately; in setups B and C its reads of the SQL endpoint show up in queryinsights without a bench tag.
- queryinsights attributes CPU time per statement with a delay of up to 15 minutes and omits system queries; columns the view does not have are reported as missing rather than guessed.
- Three repeats per question is enough to see large differences, not small ones; the bootstrap interval says how much the questions support.
- Setups B and C get only the tools Krusgraph marks read-only; its creating tools are left out, and its own `list_tables` gives way to setup A's. C's list is a judgement of which tools help answer a question, made before any run.
