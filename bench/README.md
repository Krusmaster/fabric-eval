# bench: agent efficiency with and without Krusgraph

A measurement rig. It runs the same agent over the shop lakehouse with SQL tools only (setup A), with the same SQL
tools plus Krusgraph's read-only MCP tools (setup B), and optionally with the SQL tools plus only the Krusgraph tools
that help answer a question (setup C), and records what each answer cost; `--cache` runs any of them with prompt
caching. It makes no claim either way; the report prints what was measured, with a confidence interval, and whether the
data separates the setups at all.

Status (26 October 2026): built and tested against mocks only — a replaying fake model, a SQLite stand-in for the
endpoint, fake MCP tools, the real Anthropic SDK with a mock transport, and the real MCP client against a local MCP
server on 127.0.0.1. Not yet run against a model, a workspace or krusgraph.com.

## What is in here

| file | does |
| --- | --- |
| `questions.yaml` | 25 questions over the shop schema (`fabric_backend.seed`): 8 with one relationship step, 8 with two or three, 8 that need the link `orders.ship_to_id → addresses` (5 of them, q21–q25, with further hops, where joining addresses through the customer gives another answer), 1 that needs the composite key of `order_items` |
| `truth.py` | computes every answer from `seed.rows()` in Python; nothing is typed in. `through_customer()` computes the wrong-path answers the tests compare against; `check_lakehouse()` compares the lakehouse's tables with the seed rows, row for row, before a run |
| `tools.py` | `list_tables(schema)`, `run_sql(query)`: read-only T-SQL on the SQL analytics endpoint; one statement that starts with SELECT or WITH, and no word anywhere that starts a writing, waiting or database-switching statement (T-SQL runs `SELECT 1 DROP TABLE t` as two statements); endpoint errors go to the model verbatim; every statement tagged `/* bench:<run_id>:<question_id>:r<repeat> */` |
| `mcp_tools.py` | Krusgraph's tools over MCP (streamable HTTP, MCP SDK 2.x) with the user's bearer token, sent only to https; keeps the tools the server marks read-only, leaves out the creating tools (eight at fabric-backend 10cf219) and Krusgraph's own `list_tables` (A's is kept, names stay unique); for setup C only the tools in `QA_TOOLS`; counts calls and failures |
| `agent.py` | the loop: Anthropic Messages API with tool use, structured final answer through an `answer` tool, usage and stop reasons from the API. No sampling parameters (current models answer `temperature` with a 400), `max_tokens` 16 000 per response (thinking counts toward it), effort the model's own unless `--effort` is given; with `--cache` a cache breakpoint on the system prompt (tools and system) and the API's automatic one on the conversation |
| `run.py` | runs questions × repeats for setups A, B and C (any of them, A first) and writes `results/<date>-<setup>.jsonl` (`…-cache.jsonl` with `--cache`), judged with fabric-eval's `judge` as it goes; first the seed check, which stops the run before any model call if the lakehouse differs from the seed (`--skip-seed-check` leaves it out); `--dry-run` prints the plan and an estimate and calls nothing |
| `estimate.py` | the dry run's estimate, from stated assumptions, with cache reads and writes priced when `--cache` is given; `krusgraph_tools.json` is the tool-list snapshot it uses for setups B and C (regenerate with `python -m bench.estimate --snapshot --note "main at <commit>"` where `fabric-backend[server]` is installed at that commit, and move the fabric-backend pin in `pyproject.toml`, `.github/workflows/ci.yml` and step 1 below to the same commit; a test checks the four agree) |
| `fabric_metrics.py` | afterwards: CPU time and data scanned per tagged statement from `queryinsights.exec_requests_history`, attached to the results |
| `report.py` | `REPORT.md`: share correct, a table per question tag, median and spread per correct answer (model cost at list prices included), each setup against A at the same caching setting (B − A, C − A, B+cache − A+cache …) with a bootstrap interval, optional energy/CO₂ from `factors.yaml`, limits |
| `factors.yaml` | conversion factors with source and date; empty until a source exists |

Setup B is A plus Krusgraph's read-only tools, nothing else: same model, same settings, same system prompt (the tool
list is the only difference), same questions in the same order. Setup C is A plus only the seven read-only Krusgraph
tools that help answer a question (`plan_backend`, `join_path`, `suggest_relationships`, `sample_rows`,
`describe_backend`, `query_graphql`, `answer_query_draft`; `QA_TOOLS` in `mcp_tools.py`): B's other tools draft
notebooks, views, models, dbt files and app prompts, and B resends all of them on every step. C asks how much of B's
cost is that list. The list is a judgement made before any run, not something the data chose.

`--cache` runs the chosen setups with prompt caching: the tool list and the system prompt are written to the cache on
the setup's first step and read from it afterwards, and the conversation so far is read from it on every later step.
Without it every step pays the full input price for all of that, which real clients usually avoid; with it B's long
tool list costs far less per step. Cached runs are their own setups in the report (`A+cache`, `B+cache`, `C+cache`)
and are compared only with `A+cache`.

## A first small run tonight

Five questions, one repeat, both setups: q01 (one hop), q10 (two hops), q17 (ship-to), q19 (composite key), q21
(ship-to plus two hops). About 10 agent runs.

### Before

1. The F2 capacity is **running** (not paused) and the lakehouse `demo_lh` in workspace `demo` has the shop tables:
   the seed cell has been run in a notebook attached to it, with the fabric-backend commit the rig pins (the answers
   are computed from that commit's seed rows):
   `%pip install git+https://github.com/Krusmaster/fabric-backend@10cf219d5d468a4f9138d16547d182992651f7f5` then
   `from fabric_backend.seed import seed; seed(spark, schema="shop")`.
   Every real run checks this first (one SELECT per table) and stops before any model call if a table differs; the
   message names the table and a row. A lakehouse seeded earlier with another commit passes as long as its rows are
   the same.
2. Install, on the machine that runs the rig (Python 3.11 or 3.12 and the ODBC Driver 18 for SQL Server, as
   fabric-backend needs): `pip install -e ".[test,bench]"` (it installs fabric-backend at the pinned commit).
3. Environment variables, in the shell, never in a file in the repository (bash / Git Bash):

```bash
export ANTHROPIC_API_KEY=...                                   # the Anthropic key
export FABRIC_SQL_SERVER=<xxx>.datawarehouse.fabric.microsoft.com   # demo_lh -> SQL analytics endpoint -> connection string
export FABRIC_SQL_DATABASE=demo_lh
export FABRIC_SCHEMA=shop
export FABRIC_SQL_TOKEN="$(az account get-access-token --resource https://database.windows.net/ --query accessToken -o tsv)"
# setup B only: a bearer token Krusgraph accepts (audience https://krusgraph.com/mcp, scope user_impersonation)
export KRUSGRAPH_TOKEN="$(az account get-access-token --scope https://krusgraph.com/mcp/user_impersonation --query accessToken -o tsv)"
# KRUSGRAPH_MCP_URL defaults to https://krusgraph.com/mcp; the rig refuses to send the token to a plain-http URL
```

In PowerShell the same lines are `$env:FABRIC_SQL_DATABASE = "demo_lh"` and
`$env:FABRIC_SQL_TOKEN = az account get-access-token --resource https://database.windows.net/ --query accessToken -o tsv`.

`TODO(Gustav)`: the `KRUSGRAPH_TOKEN` line assumes the Azure CLI client is allowed to request Krusgraph's scope in the
tenant (admin consent for Azure CLI on the Krusgraph app, or Azure CLI listed under the app's authorized client
applications). The server accepts the audience `https://krusgraph.com/mcp` (or the app's client id) with
`user_impersonation` in `scp` (fabric_backend/entra.py). If `az` refuses, the alternative is a device-code sign-in with a
public client that has that scope; that is a registration decision, not something the rig can do. Tokens last about an
hour; the small run takes far less, a full run may need fresh tokens between A and B.

### The run

```bash
# 1. the plan: which questions, which tools, which variables are missing, and the estimate. Calls nothing.
python -m bench.run --setups A,B --repeats 1 --only q01,q10,q17,q19,q21 --effort medium --dry-run
# 2. smoke test: one question, setup A (the seed check runs first; if it stops, re-seed before anything else)
python -m bench.run --setups A --repeats 1 --only q01 --effort medium --run-id smoke
# 3. the small run, A then B
python -m bench.run --setups A,B --repeats 1 --only q01,q10,q17,q19,q21 --effort medium
# 4. about 15 minutes later (queryinsights lag), per results file:
python -m bench.fabric_metrics --results bench/results/<date>-A.jsonl
python -m bench.fabric_metrics --results bench/results/<date>-B.jsonl --window <start ISO> <end ISO>
# 5. the report, from these two files only (the mock files sit in the same folder)
python -m bench.report bench/results/<date>-A.jsonl bench/results/<date>-B.jsonl
```

The dry run must say `set` for every variable before step 2. Step 3 prints, for setup B, the Krusgraph tools it got
from the live server and the ones it left out; compare with the dry run's list (the snapshot is of fabric-backend's
main and the deployed service can be behind it). `--effort medium` is the default of Claude Opus 5.5; giving it
explicitly puts it in every record. The model is `claude-opus-5-5` unless `--model` says otherwise; use the same for A
and B and run them back to back on the same day, so the data, the model version and the capacity state are the same.

The `--window` on the B file counts the statements in that time span that carry no bench tag: Krusgraph's own reads
of the endpoint (its `plan_backend`, `validate_plan`, `sample_rows`, `suggest_relationships`). They belong to B's cost
and are reported separately, because they cannot be attributed to a question.

### Setup C and prompt caching (optional, after the small run)

```bash
# setup C next to A and B, then the same three with prompt caching (files <date>-<setup>-cache.jsonl)
python -m bench.run --setups A,B,C --repeats 1 --only q01,q10,q17,q19,q21 --effort medium --dry-run
python -m bench.run --setups A,B,C --repeats 1 --only q01,q10,q17,q19,q21 --effort medium
python -m bench.run --setups A,B,C --repeats 1 --only q01,q10,q17,q19,q21 --effort medium --cache
# fabric metrics as above, with --window for the C files as for B; then one report over all six files
python -m bench.report bench/results/<date>-{A,B,C}.jsonl bench/results/<date>-{A,B,C}-cache.jsonl
```

Run them back to back on the same day, as with A and B. The report compares B and C with A, and `B+cache` and
`C+cache` with `A+cache`; the row "model cost USD (list price)" is the one to read for caching, since the token rows
count a token read from the cache like any other.

### Expected time and cost (estimate, not a measurement)

From the dry run on 28 October 2026 (tool snapshot of fabric-backend main at 10cf219: 36 tools, 28 read-only), at Claude Opus 5.5 list prices ($4 input / $20 output per million tokens, from
Anthropic's model table of 6 October 2026; check https://platform.claude.com/docs/en/about-claude/pricing before relying
on it):

| run | agent runs | model cost | wall time |
| --- | --- | --- | --- |
| small (5 questions × 1 repeat × A, B) | 10 | about $1.4 – $8 | 2 – 18 minutes, plus the Fabric metrics wait |
| small × A, B, C | 15 | about $1.8 – $12 | 3 – 28 minutes |
| small × A, B, C with `--cache` | 15 | about $0.4 – $5 | 3 – 28 minutes |
| full (25 questions × 3 repeats × A, B) | 150 | about $21 – $123 | 0.5 – 4.4 hours |
| full × A, B, C | 225 | about $27 – $183 | 0.7 – 6.9 hours |
| full × A, B, C with `--cache` | 225 | about $5 – $79 | 0.7 – 6.9 hours |

The range is wide because it rests on assumptions (bench/estimate.py: 3–6 steps per run in A, 4–8 in B and C, tool
results of 300–3 000 characters per step in A and 600–6 000 in B and C, 150–1 500 output tokens per step including
thinking, 3–15 s per step). Setup B resends its tool list (about 15 000–20 200 tokens, against A's 300–400 and C's
3 900–5 300) on every step, which is most of the difference in input. With `--cache` those tokens are read from the
cache at $0.20 per million on Claude Opus 5.5 (a twentieth of the input price) after the setup's first step, and each
step's new content is written to it once at 1.25 × the input price; the estimate counts both. Before and after the snapshot at 10cf219 (every parameter described, tool descriptions rewritten, `sap_pack_draft` added), same assumptions: B's tool list went from 34 648 to 59 933 characters (8 700–11 700 to 15 000–20 200 tokens, +73 %), so every step in B costs about $0.025–0.034 more without the cache. Per question without `--cache`: A $0.017–0.35 (unchanged), B $0.16–1.02 before and $0.26–1.29 after, C $0.06–0.75 before and $0.08–0.81 after; with `--cache` B $0.024–0.40 before and $0.030–0.41 after. The Fabric side is the
capacity's running time for that long; its CU use is not estimated. After the small run, the medians in the JSONL
records replace these assumptions.

## Filling in factors.yaml

When Microsoft (or a published study) gives a figure, write it with the exact document and its date:

```yaml
fabric:
  cpu_hour_kwh: { low: 0.0xx, high: 0.0yy, source: "<document title, URL>", date: "2026-11-03" }
grid:
  swedencentral: { low: 10, high: 30, source: "<document title, URL>", date: "2026-11-03" }
```

Always a range, never a point; the report multiplies the low with the low and the high with the high and prints an
interval. A factor without a source is ignored and the report says "not estimated" for anything that needs it. Token
energy per model is published by few vendors; leave it empty rather than borrowing a figure for another model.

## What the records contain

One JSON object per question and repeat: `run_id`, `setup`, `question_id`, `tags`, `repeat`, `model`, `effort`,
`max_tokens`, `started_at`, `seconds`, `steps`, `stop_reasons`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, `tokens_total`, `tool_calls` (per tool), `sql_statements`, `sql_failed`, `graphql_calls`,
`graphql_failed`, `mcp_calls`, `mcp_failed`, `answer` (`value`, `source`, `refused`, `note`), `verdict` (correct / wrong
/ refused / error; a model refusal is an error with its category), `judge_verdict` and `judge_detail` (fabric-eval's
five verdicts, for the record), `expected`, and after `fabric_metrics.py`: `fabric` (`statements`, `cpu_ms`,
`data_scanned_*_mb`, `elapsed_ms`, `columns_missing`).

Sources for the Fabric side: Microsoft's *Query Insights in Fabric Data Warehouse* page (fabric-docs,
`docs/data-warehouse/query-insights.md`, ms.date 2026-08-24): `queryinsights.exec_requests_history` holds completed
requests with `allocated_cpu_time_ms`, `data_scanned_remote_storage_mb`, `data_scanned_memory_mb`,
`data_scanned_disk_mb`, `command`, `submit_time`, `start_time`, `label`; completed queries can take up to 15 minutes to
appear; system queries are not stored. The column reference page itself was not reachable from the build
environment, so the fetcher reads `SELECT *` and lists which wanted columns are missing instead of assuming them.
