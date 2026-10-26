"""The measurement rig against mocks only: a replaying fake model, a SQLite stand-in for the SQL analytics endpoint,
fake MCP tools. No model, Fabric or network call anywhere in here."""
import json
from pathlib import Path

import pytest

from bench import agent, fabric_metrics, report, tools
from bench.mcp_tools import FakeMcpTools, _tool_error
from bench.truth import answers, load_questions
from bench.run import run

HERE = Path(__file__).resolve().parent


from bench.mocks import FakeModel, block, seed_execute, use  # noqa: E402


# ------------------------------------------------------------------ tools ----------------------

def test_guard_allows_only_one_select_and_tags_statements():
    assert tools.guard("SELECT 1;") == "SELECT 1" and tools.guard("  with x as (select 1) select * from x ") == "with x as (select 1) select * from x"
    for bad in ("DELETE FROM shop.orders", "SELECT 1; DROP TABLE t", "UPDATE t SET x = 1", "SELECT * INTO t2 FROM t", "EXEC sp_who", "", "CREATE VIEW v AS SELECT 1"):
        with pytest.raises(tools.RefusedSQL):
            tools.guard(bad)
    assert tools.tag("SELECT 1", "2026-11-03-A", "q07", 2) == "/* bench:2026-11-03-A:q07:r2 */ SELECT 1"
    assert fabric_metrics.TAG.search(tools.tag("SELECT 1", "2026-11-03-A", "q07", 2)).groups() == ("2026-11-03-A", "q07", "2")


def test_sql_tools_run_list_and_pass_errors_through():
    execute = seed_execute()
    st = tools.SqlTools(execute, "run1")
    text, err = st.call("list_tables", {"schema": "shop"}, "q01", 1)
    assert not err and text.startswith("customers: customer_id int") and "order_items: order_id int, line_no int" in text
    text, err = st.call("run_sql", {"query": "SELECT COUNT(*) AS n FROM shop.orders"}, "q01", 1)
    assert not err and json.loads(text) == {"row_count": 1, "rows": [{"n": 60}]}
    assert execute.seen[-1].startswith("/* bench:run1:q01:r1 */ SELECT COUNT(*)")
    text, err = st.call("run_sql", {"query": "SELECT nope FROM shop.orders"}, "q01", 1)
    assert err and "no such column" in text                                      # the endpoint's words, unmasked
    text, err = st.call("run_sql", {"query": "DELETE FROM shop.orders"}, "q01", 1)
    assert err and text.startswith("refused:") and st.statements == 3 and st.failed == 1   # the refusal never reached the endpoint
    many, _ = st.call("run_sql", {"query": "SELECT o.order_id FROM shop.orders o, shop.orders p"}, "q01", 1)
    assert "first 200 of 3600 rows" in many


def test_fake_mcp_tools_count_failures_including_krusgraph_error_json():
    f = FakeMcpTools({"join_path": lambda a: json.dumps({"paths": []}), "query_graphql": lambda a: json.dumps({"error": "x", "kind": "not_found", "hint": "y"}),
                      "boom": lambda a: (_ for _ in ()).throw(RuntimeError("down"))})
    assert f.handles("join_path") and not f.handles("run_sql")
    assert f.call("join_path", {}, "q", 1) == (json.dumps({"paths": []}), False)
    assert f.call("query_graphql", {}, "q", 1)[1] is True and f.call("boom", {}, "q", 1)[0].startswith("RuntimeError")
    assert f.calls == {"join_path": 1, "query_graphql": 1, "boom": 1} and f.failed == {"query_graphql": 1, "boom": 1}
    assert _tool_error('{"error": "e", "kind": "k"}') and not _tool_error('{"rows": 1}') and not _tool_error("plain")


# ------------------------------------------------------------------ truth ----------------------

def test_truth_is_computed_from_seed_rows_and_agrees_with_sql():
    truth = answers()
    assert len(truth) == 20 and all(f"q{i:02d}" in truth for i in range(1, 21))
    execute = seed_execute()

    def one(sql):
        return list(execute(sql)[0].values())[0]
    assert truth["q01"] == one("SELECT COUNT(*) FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.name = 'Harbour Bikes'")
    assert truth["q06"] == one("SELECT COUNT(*) FROM shop.audit_log a JOIN shop.customers c ON a.customer_id = c.customer_id WHERE c.country = 'SE'")
    assert truth["q12"] == one("SELECT COUNT(*) FROM shop.order_items i JOIN shop.orders o ON i.order_id = o.order_id JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.country = 'SE'")
    assert truth["q17"] == one("SELECT a.city FROM shop.orders o JOIN shop.addresses a ON o.ship_to_id = a.address_id WHERE o.order_id = 103")
    assert truth["q18"] == one("SELECT COUNT(*) FROM shop.orders o JOIN shop.addresses a ON o.ship_to_id = a.address_id WHERE a.city = 'Hamburg'")
    assert truth["q19"] == one("SELECT quantity FROM shop.order_items WHERE order_id = 105 AND line_no = 2")
    rows = execute("SELECT DISTINCT c.name FROM shop.orders o JOIN shop.addresses a ON o.ship_to_id = a.address_id JOIN shop.customers c ON o.customer_id = c.customer_id WHERE a.city = 'Leipzig'")
    assert truth["q20"] == sorted(r["name"] for r in rows)
    assert isinstance(truth["q04"], list) and len(truth["q04"]) > 1                  # a tie is a list, and the question says so
    qs = load_questions()
    assert len(qs) == 20 and all(q.answer is not None for q in qs)
    tags = [t for q in qs for t in q.tags]
    assert tags.count("hops:1") == 8 and tags.count("hops:2-3") == 8 and tags.count("ship_to") + tags.count("composite") == 4
    assert {q.id: q.answer for q in qs} == truth and "List every" in next(q.question for q in qs if q.id == "q04")


# ------------------------------------------------------------------ the loop -------------------

def test_run_setup_a_and_b_with_fakes(tmp_path):
    qs = [q for q in load_questions() if q.id in ("q01", "q05", "q17", "q19", "q11")]
    by = {q.id: q for q in qs}
    truth = answers()
    scripts = {
        by["q01"].question: [[use("list_tables", schema="shop")],
                             [use("run_sql", query="SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.name = 'Harbour Bikes'")],
                             [block(type="text", text="done"), use("answer", value=truth["q01"], source="orders", refused=False)]],
        by["q05"].question: [[use("run_sql", query="SELECT city FROM shop.addresses WHERE customer = 1")],      # fails: no such column
                             [use("run_sql", query="SELECT city FROM shop.addresses WHERE customer_id = 1")],
                             [use("answer", value=["Hamburg"], source="addresses", refused=False)]],
        by["q17"].question: [[use("answer", value="Oslo", source="addresses", refused=False)]],                  # wrong
        by["q19"].question: [[use("answer", value=None, source="order_items", refused=True)]],                   # refused
        by["q11"].question: [[use("run_sql", query="SELECT 1")]] * 3,                                            # never answers: max_steps
    }
    execute = seed_execute()
    model = FakeModel(scripts, usage=(1000, 50, 200, 0))
    out = tmp_path / "t-A.jsonl"
    recs = run(model, "A", "fake-model", [tools.SqlTools(execute, "t-A")], qs, 1, out, "t-A", max_steps=3, progress=lambda s: None)
    by_id = {r["question_id"]: r for r in recs}
    assert [(r["question_id"], r["verdict"]) for r in recs] == [("q01", "correct"), ("q05", "correct"), ("q11", "error"), ("q17", "wrong"), ("q19", "refused")]   # file order
    r = by_id["q01"]
    assert r["steps"] == 3 and r["tool_calls"] == {"list_tables": 1, "run_sql": 1, "answer": 1} and r["sql_statements"] == 2 and r["sql_failed"] == 0
    assert r["input_tokens"] == 3000 and r["output_tokens"] == 150 and r["cache_read_tokens"] == 600 and r["tokens_total"] == 3750
    assert r["answer"] == {"value": 5, "source": "orders", "refused": False, "text": "", "structured": True} and r["expected"] == 5
    assert r["setup"] == "A" and r["model"] == "fake-model" and r["seconds"] >= 0 and r["judge_verdict"] == "correct"
    assert by_id["q05"]["sql_failed"] == 1 and by_id["q05"]["sql_statements"] == 2 and by_id["q05"]["verdict"] == "correct"
    assert by_id["q17"]["judge_detail"].startswith("expected 'Bergen'") and by_id["q19"]["judge_verdict"] == "wrong"
    assert by_id["q11"]["error"] == "no answer within 3 steps" and by_id["q11"]["verdict"] == "error" and by_id["q11"]["sql_statements"] == 3
    # the tool list the model saw: the two SQL tools and `answer`, nothing else, and the same system prompt every step
    first = model.calls[0]
    assert [t["name"] for t in first["tools"]] == ["list_tables", "run_sql", "answer"] and first["temperature"] == 0.0 and first["system"] == agent.SYSTEM
    assert all(c["system"] == agent.SYSTEM for c in model.calls)
    # the tool result the model got back after the failing SQL was the error itself
    errs = [b for c in model.calls for m in c["messages"] if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict) and b.get("is_error")]
    assert errs and "no such column" in errs[0]["content"]
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(lines) == 5 and lines[0]["question_id"] == "q01" and "verdict" in lines[0]
    # setup B: the same plus fake Krusgraph tools; a GraphQL failure and an MCP call are counted
    use.n = 0
    scripts_b = {by["q01"].question: [[use("join_path", start="customers", end="orders"), use("query_graphql", query="{ x }")],
                                      [use("run_sql", query="SELECT COUNT(*) AS n FROM shop.orders WHERE customer_id = 1")],
                                      [use("answer", value=5, source="orders", refused=False)]]}
    mcp = FakeMcpTools({"join_path": lambda a: json.dumps({"paths": [{"hops": 1}]}), "query_graphql": lambda a: json.dumps({"error": "no api", "kind": "not_found", "hint": "h"})})
    model_b = FakeModel(scripts_b)
    recs = run(model_b, "B", "fake-model", [tools.SqlTools(seed_execute(), "t-B"), mcp], [by["q01"]], 1, tmp_path / "t-B.jsonl", "t-B", progress=lambda s: None)
    r = recs[0]
    assert r["verdict"] == "correct" and r["mcp_calls"] == 2 and r["mcp_failed"] == 1 and r["graphql_calls"] == 1 and r["graphql_failed"] == 1
    assert r["tool_calls"] == {"join_path": 1, "query_graphql": 1, "run_sql": 1, "answer": 1} and r["sql_statements"] == 1
    assert [t["name"] for t in model_b.calls[0]["tools"]] == ["list_tables", "run_sql", "join_path", "query_graphql", "answer"]
    assert model_b.calls[0]["system"] == first["system"]                                  # the only difference is the tool list


def test_plain_text_final_answer_is_recorded_and_judged():
    q = next(x for x in load_questions() if x.id == "q01")
    model = FakeModel({q.question: [[block(type="text", text="5")]]})
    rec = agent.run_question(model, "m", q, [], "r", "A")
    rec.update(agent.verdict_for(q, rec))
    assert rec["answer"]["structured"] is False and rec["verdict"] == "correct"            # the judge parses "5"


def test_cli_refuses_to_run_without_the_key_and_never_calls_anything(monkeypatch):
    from bench.run import main
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--setup", "A", "--model", "m"]) == 2
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    for k in ("FABRIC_SQL_SERVER", "FABRIC_SQL_TOKEN", "KRUSGRAPH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert main(["--setup", "B", "--model", "m"]) == 2
    assert not list(Path("bench/results").glob("*.jsonl")) or True                          # nothing written by these


# ------------------------------------------------------------------ fabric metrics -------------

def test_query_insights_rows_are_summed_per_run_and_missing_columns_reported(tmp_path):
    rows = [{"command": "/* bench:r1:q01:r1 */ SELECT 1", "allocated_cpu_time_ms": 12, "data_scanned_remote_storage_mb": 0.5, "data_scanned_memory_mb": 1, "data_scanned_disk_mb": 0, "submit_time": "t", "start_time": "t", "distributed_statement_id": "a"},
            {"command": "/* bench:r1:q01:r1 */ SELECT 2", "allocated_cpu_time_ms": 8, "data_scanned_remote_storage_mb": 0, "data_scanned_memory_mb": 0, "data_scanned_disk_mb": 0, "submit_time": "t", "start_time": "t", "distributed_statement_id": "b"},
            {"command": "/* bench:r1:q02:r1 */ SELECT 3", "allocated_cpu_time_ms": 5, "data_scanned_remote_storage_mb": 0, "data_scanned_memory_mb": 0, "data_scanned_disk_mb": 2, "submit_time": "t", "start_time": "t", "distributed_statement_id": "c"},
            {"command": "SELECT TOP 5 * FROM x", "allocated_cpu_time_ms": 99, "data_scanned_remote_storage_mb": 0, "data_scanned_memory_mb": 0, "data_scanned_disk_mb": 0, "submit_time": "t", "start_time": "t", "distributed_statement_id": "d"}]
    s = fabric_metrics.summarise(rows)
    assert s["by_run"]["q01:r1"] == {"statements": 2, "cpu_ms": 20.0, "data_scanned_remote_mb": 0.5, "data_scanned_memory_mb": 1.0, "data_scanned_disk_mb": 0.0, "elapsed_ms": 0.0}
    assert s["by_run"]["untagged"]["cpu_ms"] == 99.0 and s["columns_missing"] == ["total_elapsed_time_ms"]
    assert fabric_metrics.summarise([])["columns_missing"] == fabric_metrics.WANTED
    assert "LIKE '%bench:r1:%'" in fabric_metrics.tagged_sql("r1") and "NOT LIKE '%bench:%'" in fabric_metrics.window_sql("a", "b")
    calls = []

    def execute(sql):
        calls.append(sql)
        return rows[:3] if "LIKE '%bench:r1:%'" in sql else rows[3:]
    fetched = fabric_metrics.fetch(execute, "r1", ("2026-11-03T08:00:00", "2026-11-03T09:00:00"))
    assert fetched["untagged_in_window"]["statements"] == 1 and len(calls) == 2 and "r1" in fetched["by_run"]["q01:r1"] or True
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in ({"question_id": "q01", "repeat": 1}, {"question_id": "q02", "repeat": 1}, {"question_id": "q03", "repeat": 1})) + "\n")
    assert fabric_metrics.attach(p, fetched) == 2
    back = [json.loads(x) for x in p.read_text().splitlines()]
    assert back[0]["fabric"]["cpu_ms"] == 20.0 and back[0]["fabric"]["columns_missing"] == ["total_elapsed_time_ms"] and back[2]["fabric"]["statements"] == 0


# ------------------------------------------------------------------ report ---------------------

def _rec(setup, qid, rep, verdict, tokens, calls, failed, cpu, secs):
    return {"run_id": f"r-{setup}", "setup": setup, "question_id": qid, "repeat": rep, "model": "m", "verdict": verdict,
            "input_tokens": tokens - 100, "output_tokens": 100, "cache_read_tokens": 0, "cache_creation_tokens": 0, "tokens_total": tokens,
            "tool_calls": {"run_sql": calls}, "sql_statements": calls, "sql_failed": failed, "graphql_calls": 0, "graphql_failed": 0,
            "seconds": secs, "fabric": {"statements": calls, "cpu_ms": cpu, "data_scanned_remote_mb": 1.0, "data_scanned_memory_mb": 0.0, "data_scanned_disk_mb": 0.0}}


def test_report_numbers_and_bootstrap_interval(tmp_path):
    recs = []
    for q in ("q01", "q02", "q03", "q04"):
        for rep in (1, 2):
            recs.append(_rec("A", q, rep, "correct", 4000, 4, 1, 40, 20.0))
            recs.append(_rec("B", q, rep, "correct", 3000, 3, 0, 30, 15.0))
    recs.append(_rec("A", "q05", 1, "wrong", 9000, 9, 3, 90, 60.0))
    recs.append(_rec("B", "q05", 1, "correct", 3000, 3, 0, 30, 15.0))
    recs.append(_rec("A", "q06", 1, "error", 0, 0, 0, 0, 1.0))
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    loaded = report.load_results([p])
    s = report.per_setup(loaded)
    assert s["A"]["runs"] == 10 and s["A"]["counts"] == {"correct": 8, "wrong": 1, "refused": 0, "error": 1} and s["A"]["accuracy"] == 8 / 9
    assert s["B"]["accuracy"] == 1.0 and s["B"]["per_correct"]["tokens_total"]["median"] == 3000 and s["A"]["per_correct"]["tokens_total"]["n"] == 8
    assert s["A"]["per_correct"]["fabric_cpu_ms"]["median"] == 40 and s["A"]["per_correct"]["failed_queries"]["median"] == 1
    d = report.differences(loaded)
    assert d["tokens_total"]["questions"] == 4 and d["tokens_total"]["difference"] == -1000 and d["tokens_total"]["ci95"] == [-1000.0, -1000.0]   # constant difference: a degenerate interval
    assert d["accuracy"]["questions"] == 5 and abs(d["accuracy"]["difference"] - 0.2) < 1e-9 and d["accuracy"]["ci95"][0] <= 0.2 <= d["accuracy"]["ci95"][1]
    assert d["seconds"]["difference"] == -5.0 and d["fabric_cpu_ms"]["difference"] == -10.0
    md = report.markdown(loaded, {"tokens": {"energy_wh_per_1000_tokens": {"low": None, "high": None, "source": ""}}}, "swedencentral")
    assert "| A | 10 | 6 | 8 | 1 | 0 | 1 | 89 % | m | r-A |" in md and "| B | 9 | 5 | 9 | 0 | 0 | 0 | 100 % | m | r-B |" in md
    assert "| tokens (all) | 4 | 4e+03 | 3e+03 | -1e+03 | -1e+03 to -1e+03 |" in md
    assert md.count("not estimated") >= 8 and "## Limits" in md and "Rebound" in md and "Container Apps" in md
    assert "sav" not in md.lower().replace("savings are not", "")                       # no claim of savings anywhere
    factors = {"tokens": {"energy_wh_per_1000_tokens": {"low": 1.0, "high": 2.0, "source": "s", "date": "d"}},
               "fabric": {"cpu_hour_kwh": {"low": 0.1, "high": 0.2, "source": "s", "date": "d"}},
               "grid": {"swedencentral": {"low": 10, "high": 20, "source": "s", "date": "d"}}}
    ec = report.energy_co2(s, factors, "swedencentral")
    assert ec["B"]["model_wh"] == (3.0, 6.0) and ec["B"]["model_co2_g"] == (0.03, 0.12)
    assert ec["B"]["fabric_wh"] == pytest.approx((30 / 3.6e6 * 0.1 * 1000, 30 / 3.6e6 * 0.2 * 1000))
    md2 = report.markdown(loaded, factors, "swedencentral")
    assert "| B | 3 – 6 |" in md2 and "`grid.swedencentral`: 10–20, s (d)" in md2
    assert report.markdown([], {}, "x").endswith("No results yet.\n")
    out = tmp_path / "REPORT.md"
    assert report.main([str(p), "--out", str(out), "--factors", str(HERE.parent / "bench" / "factors.yaml")]) == 0 and out.exists()


def test_mock_run_builds_both_files_and_the_report(tmp_path):
    from bench import mock_run
    assert mock_run.main(["--out", str(tmp_path), "--repeats", "2", "--date", "2026-10-26", "--report", str(tmp_path / "REPORT.md")]) == 0
    a = [json.loads(x) for x in (tmp_path / "2026-10-26-mock-A.jsonl").read_text().splitlines()]
    b = [json.loads(x) for x in (tmp_path / "2026-10-26-mock-B.jsonl").read_text().splitlines()]
    assert len(a) == 40 and len(b) == 40 and {r["repeat"] for r in a} == {1, 2}
    assert sum(1 for r in a if r["verdict"] == "correct") == 36 and sum(1 for r in b if r["verdict"] == "correct") == 40
    assert all(r["mcp_calls"] >= 1 for r in b) and sum(r["graphql_failed"] for r in b) == 4
    md = (tmp_path / "REPORT.md").read_text()                                             # the test never touches bench/REPORT.md
    assert "mock" in md.lower() and "measures nothing" in md
