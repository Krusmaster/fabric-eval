# bench: agent efficiency with and without Krusgraph

A measurement rig. It runs the same agent over the shop lakehouse twice — once with SQL tools only (setup A), once
with the same SQL tools plus Krusgraph's MCP tools (setup B) — and records what each answer cost. It makes no claim
about savings; the report prints what was measured, with a confidence interval, whether the difference is large,
small or none.

## What is in here

| file | does |
| --- | --- |
| `questions.yaml` | 20 questions over the shop schema (`fabric_backend.seed`): 8 with one relationship step, 8 with two or three, 4 that need the confirmed link `orders.ship_to_id → addresses` or the composite key of `order_items` |
| `truth.py` | computes every answer from `seed.rows()` in Python; nothing is typed in |
| `tools.py` | `list_tables(schema)`, `run_sql(query)`: read-only T-SQL on the SQL analytics endpoint; refuses anything but SELECT; endpoint errors go to the model verbatim; tags every statement `/* bench:<run_id>:<question_id>:r<repeat> */` |
| `mcp_tools.py` | Krusgraph's tools over MCP (streamable HTTP) with the user's bearer token; counts calls and failures |
| `agent.py` | the loop: Anthropic Messages API with tool use, structured final answer through an `answer` tool, usage from the API |
| `run.py` | runs questions × repeats for one setup and writes `results/<date>-<setup>.jsonl`, judged with fabric-eval's `judge` as it goes |
| `fabric_metrics.py` | afterwards: CPU time and data scanned per tagged statement from `queryinsights.exec_requests_history`, attached to the results |
| `report.py` | `REPORT.md`: share correct, median and spread per correct answer, B − A with a bootstrap interval, optional energy/CO₂ from `factors.yaml`, limits |
| `factors.yaml` | conversion factors with source and date; empty until a source exists |

Setup B is A plus Krusgraph, nothing else: same model, same system prompt (the tool list is the only difference),
same temperature (0), same questions in the same order, three repeats per question.

Status: built and tested against mocks only (a replaying fake model, a SQLite stand-in for the endpoint, fake MCP
tools). Not yet run against a model or a workspace.

## Running it (tomorrow)

Before the run:

1. The F2 capacity is **running** (not paused) and the lakehouse `demo_lh` in workspace `demo` has the shop tables:
   the seed cell has been run (`from fabric_backend.seed import seed; seed(spark, schema="shop")`).
2. Install: `pip install -e ".[test,bench]"` (needs the ODBC Driver 18 for SQL Server on the machine, as fabric-backend does).
3. Environment variables, in the shell, never in a file in the repository:

```bash
export ANTHROPIC_API_KEY=...                                   # the Anthropic key
export FABRIC_SQL_SERVER=<xxx>.datawarehouse.fabric.microsoft.com   # demo_lh -> SQL analytics endpoint -> connection string
export FABRIC_SQL_DATABASE=demo_lh
export FABRIC_SCHEMA=shop
export FABRIC_SQL_TOKEN="$(az account get-access-token --resource https://database.windows.net/ --query accessToken -o tsv)"
# setup B only: a bearer token Krusgraph accepts (audience https://krusgraph.com/mcp, scope user_impersonation)
export KRUSGRAPH_TOKEN="$(az account get-access-token --scope https://krusgraph.com/mcp/user_impersonation --query accessToken -o tsv)"
export KRUSGRAPH_MCP_URL=https://krusgraph.com/mcp
```

`TODO(Gustav)`: the `KRUSGRAPH_TOKEN` line assumes the Azure CLI client is allowed to request Krusgraph's scope in the
tenant (admin consent for Azure CLI on the Krusgraph app, or Azure CLI listed under the app's authorized client
applications). If `az` refuses, the alternative is a device-code sign-in with a public client that has that scope;
that is a registration decision, not something the rig can do. Tokens last about an hour; a full setup takes less.

Smoke test first (one question, one repeat, setup A), then the real thing:

```bash
python -m bench.run --setup A --model <model id> --repeats 1 --only q01
python -m bench.run --setup A --model <model id> --repeats 3
python -m bench.run --setup B --model <model id> --repeats 3
# 15 minutes later (queryinsights lag), per results file:
python -m bench.fabric_metrics --results bench/results/<date>-A.jsonl
python -m bench.fabric_metrics --results bench/results/<date>-B.jsonl --window <start ISO> <end ISO>
python -m bench.report            # writes bench/REPORT.md from bench/results/*.jsonl
```

The `--window` on the B file counts the statements in that time span that carry no bench tag: Krusgraph's own reads
of the endpoint (its `list_tables`, `plan_backend`, `validate_plan`, `sample_rows`). They belong to B's cost and are
reported separately, because they cannot be attributed to a question.

The model id is a parameter (`--model`); use the same for A and B. Run A and B back to back on the same day so the
data, the model version and the capacity state are the same.

## What a run costs and takes (estimate, not a measurement)

Volume: 20 questions × 3 repeats × 2 setups = 120 agent runs. Tokens per run depend on the model and the transcript;
a rough budget from the tool schemas and typical step counts:

| | steps per run | input tokens per run | output tokens per run | runs | input total | output total |
| --- | --- | --- | --- | --- | --- | --- |
| A (2 tools + answer) | ~5 | ~20 000 (system + tools + growing transcript, before cache) | ~1 500 | 60 | ~1.2 M | ~0.09 M |
| B (A + 22 Krusgraph tools) | ~5 | ~50 000 (the tool list alone is several thousand tokens per step) | ~1 500 | 60 | ~3.0 M | ~0.09 M |

That is about 4 million input and 0.2 million output tokens for the whole run. At list prices in the order of a few
dollars per million input tokens and 15 per million output tokens (Sonnet class; check the current price list, an
Opus-class model is several times that) the API cost is in the order of **15–20 dollars**, less with prompt caching
on repeats 2 and 3. Wall time: 30–90 seconds per run sequentially, so 1–3 hours for the 120 runs; the Fabric side is
one F2 capacity hour or two. All of this is an estimate; the JSONL records hold the real numbers afterwards.

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

One JSON object per question and repeat: `run_id`, `setup`, `question_id`, `repeat`, `model`, `started_at`, `seconds`,
`steps`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `tokens_total`, `tool_calls`
(per tool), `sql_statements`, `sql_failed`, `graphql_calls`, `graphql_failed`, `mcp_calls`, `mcp_failed`, `answer`
(`value`, `source`, `refused`, `note`), `verdict` (correct / wrong / refused / error), `judge_verdict` and `judge_detail`
(fabric-eval's five verdicts, for the record), `expected`, and after `fabric_metrics.py`: `fabric` (`statements`,
`cpu_ms`, `data_scanned_*_mb`, `elapsed_ms`, `columns_missing`).

Sources for the Fabric side: Microsoft's *Query Insights in Fabric Data Warehouse* page (fabric-docs,
`docs/data-warehouse/query-insights.md`, ms.date 2026-08-24): `queryinsights.exec_requests_history` holds completed
requests with `allocated_cpu_time_ms`, `data_scanned_remote_storage_mb`, `data_scanned_memory_mb`,
`data_scanned_disk_mb`, `command`, `submit_time`, `start_time`, `label`; completed queries can take up to 15 minutes to
appear; system queries are not stored. The column reference page itself was not reachable from the build
environment, so the fetcher reads `SELECT *` and lists which wanted columns are missing instead of assuming them.
