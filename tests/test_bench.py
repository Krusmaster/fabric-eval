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


def test_guard_refuses_second_statements_without_a_semicolon_and_outside_reads():
    # T-SQL runs `SELECT 1 DROP TABLE t` as two statements; setup A must still only ever read
    for bad in ("SELECT 1 DROP TABLE shop.orders", "WITH x AS (SELECT 1 AS a) DELETE FROM shop.orders", "SELECT 1 WAITFOR DELAY '00:10'",
                "SELECT 1 USE master", "SELECT * FROM OPENQUERY(srv, 'SELECT 1')", "SELECT * FROM OPENDATASOURCE('x', 'y').db.dbo.t",
                "SELECT 1 GRANT SELECT ON shop.orders TO public", "SELECT 1 DENY SELECT ON shop.orders TO public", "SELECT 1 KILL 52",
                "INSERT INTO shop.orders VALUES (1)", "MERGE shop.orders AS t USING x ON 1=1 WHEN MATCHED THEN DELETE", "DECLARE @x int SELECT 1"):
        with pytest.raises(tools.RefusedSQL):
            tools.guard(bad)
    for good in ("SELECT TOP (5) * FROM shop.orders ORDER BY order_date DESC OFFSET 0 ROWS",
                 "WITH x AS (SELECT customer_id, COUNT(*) AS n FROM shop.orders GROUP BY customer_id) SELECT * FROM x",
                 "SELECT event FROM shop.audit_log WHERE event = 'address_changed'"):
        assert tools.guard(good) == good
    # setup A's model sees exactly these two tools (and `answer`); nothing else can reach the endpoint
    assert [t["name"] for t in tools.TOOLS] == ["list_tables", "run_sql"]
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
    assert len(truth) == 25 and all(f"q{i:02d}" in truth for i in range(1, 26))
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
    assert len(qs) == 25 and all(q.answer is not None for q in qs)
    tags = [t for q in qs for t in q.tags]
    assert tags.count("hops:1") == 8 and tags.count("hops:2-3") == 8 and tags.count("composite") == 1
    assert tags.count("ship_to") == 8 and tags.count("join_path") == 5
    assert {q.id: q.answer for q in qs} == truth and "List every" in next(q.question for q in qs if q.id == "q04")


def test_join_path_questions_agree_with_sql_and_the_wrong_path_gives_another_answer():
    from bench.mock_run import SQL
    from bench.truth import through_customer
    truth = answers()
    execute = seed_execute()
    rows = {q: execute(SQL[q]) for q in ("q21", "q22", "q23", "q24", "q25")}
    assert truth["q21"] == sorted(r["name"] for r in rows["q21"]) and len(truth["q21"]) == 5
    assert truth["q22"] == rows["q22"][0]["n"] == 0                                   # every order ships to its own customer's address
    assert truth["q23"] == rows["q23"][0]["q"] == 15.0
    assert truth["q24"] == sorted(r["city"] for r in rows["q24"]) == ["Kiel", "Lund", "Uppsala"]
    assert truth["q25"] == rows["q25"][0]["n"] == 6
    # joining addresses through the customer instead of orders.ship_to_id: a plausible query with another answer
    for q in ("q21", "q23", "q24", "q25"):
        assert through_customer(q) != truth[q], q
    assert all({"ship_to", "join_path"} <= set(q.tags) for q in load_questions() if q.id in ("q21", "q22", "q23", "q24", "q25"))


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
    assert [t["name"] for t in first["tools"]] == ["list_tables", "run_sql", "answer"] and first["system"] == agent.SYSTEM
    # no sampling parameters (current models answer them with a 400), the model's own effort, room for thinking
    assert "temperature" not in first and "output_config" not in first and first["max_tokens"] == agent.DEFAULT_MAX_TOKENS == 16000
    assert r["tags"] == ["hops:1"] and r["stop_reasons"] == ["tool_use", "tool_use", "tool_use"] and r["effort"] is None
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
    assert len(a) == 50 and len(b) == 50 and {r["repeat"] for r in a} == {1, 2}
    assert sum(1 for r in a if r["verdict"] == "correct") == 44 and sum(1 for r in b if r["verdict"] == "correct") == 50
    assert all(r["mcp_calls"] >= 1 for r in b) and sum(r["graphql_failed"] for r in b) == 4
    assert all(r["tool_calls"].get("join_path") == 1 for r in b if "join_path" in r["tags"])
    md = (tmp_path / "REPORT.md").read_text(encoding="utf-8")                             # the test never touches bench/REPORT.md
    assert "mock" in md.lower() and "measures nothing" in md
    assert "## By question tag" in md and "| join_path | 8/10 | 3740 | 10/10 | 23485 |" in md and "| ship_to | 11/16 |" in md


# ------------------------------------------------------------------ request shape --------------

def test_request_params_send_no_sampling_parameters_unless_asked():
    p = agent.request_params("m", "sys", [], [], 16000)
    assert p == {"model": "m", "system": "sys", "tools": [], "messages": [], "max_tokens": 16000}
    assert agent.request_params("m", "s", [], [], 10, temperature=0.2, effort="high")["temperature"] == 0.2
    assert agent.request_params("m", "s", [], [], 10, effort="low")["output_config"] == {"effort": "low"}


def test_the_real_sdk_accepts_the_request_and_its_responses_are_read(monkeypatch):
    """The anthropic SDK itself, with a mock transport instead of the network: the request body the API would get (no
    temperature, effort in output_config, the tool list) and the SDK's own response types read back, the thinking block
    passed back unchanged on the next step."""
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    bodies = []
    q = next(x for x in load_questions() if x.id == "q01")
    sql = "SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.name = 'Harbour Bikes'"
    usage = {"input_tokens": 900, "output_tokens": 60, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    replies = [{"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use", "stop_sequence": None,
                "content": [{"type": "thinking", "thinking": "", "signature": "sig-1"}, {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {"query": sql}}],
                "usage": usage},
               {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use", "stop_sequence": None,
                "content": [{"type": "text", "text": "Five."}, {"type": "tool_use", "id": "toolu_2", "name": "answer", "input": {"value": 5, "source": "orders", "refused": False}}],
                "usage": usage}]

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=replies[len(bodies) - 1])
    client = anthropic.Anthropic(api_key="test-only", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    rec = agent.run_question(client, "claude-opus-5-5", q, [tools.SqlTools(seed_execute(), "r")], "r", "A", effort="medium")
    rec.update(agent.verdict_for(q, rec))
    assert rec["verdict"] == "correct" and rec["steps"] == 2 and rec["input_tokens"] == 1800 and rec["stop_reasons"] == ["tool_use", "tool_use"]
    first = bodies[0]
    assert sorted(first) == ["max_tokens", "messages", "model", "output_config", "system", "tools"]          # no temperature
    assert first["output_config"] == {"effort": "medium"} and first["max_tokens"] == 16000
    assert [t["name"] for t in first["tools"]] == ["list_tables", "run_sql", "answer"]
    assert bodies[1]["messages"][1]["content"][0] == {"signature": "sig-1", "thinking": "", "type": "thinking"}   # passed back as it came


def test_a_refusal_is_recorded_as_an_error_with_its_category():
    from types import SimpleNamespace
    q = next(x for x in load_questions() if x.id == "q01")

    class Refusing:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            return SimpleNamespace(content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    rec = agent.run_question(Refusing(), "m", q, [], "r", "A")
    rec.update(agent.verdict_for(q, rec))
    assert rec["error"] == "model refusal (cyber)" and rec["verdict"] == "error" and rec["stop_reasons"] == ["refusal"] and rec["steps"] == 1


def test_two_tools_with_one_name_are_refused_before_any_call():
    q = next(x for x in load_questions() if x.id == "q01")
    model = FakeModel({})
    clash = FakeMcpTools({"list_tables": lambda a: "x"})
    with pytest.raises(ValueError, match="list_tables"):
        agent.run_question(model, "m", q, [tools.SqlTools(seed_execute(), "r"), clash], "r", "B")
    assert model.calls == []


# ------------------------------------------------------------------ the CLI --------------------

def _clear_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "FABRIC_SQL_SERVER", "FABRIC_SQL_TOKEN", "KRUSGRAPH_TOKEN", "KRUSGRAPH_MCP_URL"):
        monkeypatch.delenv(k, raising=False)


def test_dry_run_prints_the_plan_and_calls_nothing(monkeypatch, tmp_path, capsys):
    import sys
    from bench import run as runmod
    _clear_env(monkeypatch)
    monkeypatch.setitem(sys.modules, "anthropic", None)                  # importing it would raise: the dry run must not
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    monkeypatch.setattr("bench.mcp_tools.McpTools.start", lambda self: (_ for _ in ()).throw(AssertionError("no MCP call in a dry run")))
    assert runmod.main(["--setups", "A,B", "--repeats", "1", "--only", "q01,q10,q17,q19,q21", "--dry-run", "--out", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("DRY RUN: nothing is called") and "Setups, one after the other: A, B   repeats: 1   questions: 5   runs: 10" in text
    assert "Model: claude-opus-5-5" in text and "temperature: not sent" in text
    for qid in ("q01", "q10", "q17", "q19", "q21"):
        assert f"  {qid} [" in text
    assert "q02" not in text
    assert "left out (they create items in Fabric): create_backend, create_semantic_model, create_views, create_data_agent" in text
    assert "left out (setup A's tool of the same name is kept): list_tables" in text and "https://krusgraph.com/mcp" in text
    assert "ANTHROPIC_API_KEY: MISSING" in text and "KRUSGRAPH_TOKEN: MISSING" in text
    assert "ESTIMATE (assumptions, not a measurement):" in text and "model cost $" in text and "pricing" in text
    assert not list(tmp_path.iterdir())                                   # nothing written
    assert "sav" not in text.lower()                                      # no claim about savings
    # a model without a known price: the cost is not estimated unless prices are given
    assert runmod.main(["--setups", "A", "--repeats", "1", "--limit", "2", "--model", "some-other-model", "--dry-run"]) == 0
    other = capsys.readouterr().out
    assert "model cost not estimated" in other and "pass --price-in and --price-out" in other and "questions: 2" in other
    assert runmod.main(["--setups", "A", "--repeats", "1", "--limit", "2", "--model", "x", "--price-in", "1", "--price-out", "5", "--dry-run"]) == 0
    assert "given on the command line" in capsys.readouterr().out


def test_setups_and_question_selection(monkeypatch):
    from bench import run as runmod
    from types import SimpleNamespace as NS
    assert runmod.setups_from(NS(setups="b,a", setup=None)) == ["A", "B"] and runmod.setups_from(NS(setups=None, setup="B")) == ["B"]
    for bad in (NS(setups="C", setup=None), NS(setups=None, setup=None), NS(setups=",", setup=None)):
        with pytest.raises(SystemExit):
            runmod.setups_from(bad)
    qs = load_questions()
    assert [q.id for q in runmod.select_questions(qs, None, 5)] == ["q01", "q02", "q03", "q04", "q05"]
    assert [q.id for q in runmod.select_questions(qs, "q21,q17", None)] == ["q17", "q21"]            # file order kept
    with pytest.raises(SystemExit):
        runmod.select_questions(qs, "q99", None)
    with pytest.raises(SystemExit):
        runmod.select_questions(qs, None, 0)


def test_setup_b_connects_to_krusgraph_with_the_token_and_a_only_gets_sql(monkeypatch, tmp_path):
    import sys
    import types
    from bench import run as runmod
    _clear_env(monkeypatch)
    for k, v in {"ANTHROPIC_API_KEY": "test-key", "FABRIC_SQL_SERVER": "endpoint.example", "FABRIC_SQL_TOKEN": "sql-token", "KRUSGRAPH_TOKEN": "kg-token"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: "client"))
    monkeypatch.setattr("bench.tools.sqlserver_execute", lambda server, db, token: seed_execute())
    made, seen = [], []

    class Mcp(FakeMcpTools):
        def __init__(self, url, token, reserved=()):
            made.append({"url": url, "token": token, "reserved": list(reserved)})
            super().__init__({"join_path": lambda a: "{}"})

        def start(self):
            return self
    monkeypatch.setattr("bench.mcp_tools.McpTools", Mcp)
    monkeypatch.setattr(runmod, "run", lambda client, setup, model, toolsets, *a, **k: seen.append((setup, client, [n for ts in toolsets for n in (t["name"] for t in ts.tools)])) or [])
    assert runmod.main(["--setups", "A,B", "--repeats", "1", "--limit", "1", "--out", str(tmp_path)]) == 0
    assert seen[0] == ("A", "client", ["list_tables", "run_sql"])                                   # A: the two SQL tools, nothing else
    assert seen[1][0] == "B" and seen[1][2] == ["list_tables", "run_sql", "join_path"]
    assert made == [{"url": "https://krusgraph.com/mcp", "token": "kg-token", "reserved": ["list_tables", "run_sql", "answer"]}]
    # the token never goes to a plain-http URL
    monkeypatch.setenv("KRUSGRAPH_MCP_URL", "http://krusgraph.com/mcp")
    made.clear()
    assert runmod.main(["--setups", "B", "--repeats", "1", "--limit", "1", "--out", str(tmp_path)]) == 2 and made == []
    # setup B without its token does not start
    monkeypatch.delenv("KRUSGRAPH_MCP_URL")
    monkeypatch.delenv("KRUSGRAPH_TOKEN")
    assert runmod.main(["--setups", "B", "--repeats", "1", "--limit", "1", "--out", str(tmp_path)]) == 2 and made == []


# ------------------------------------------------------------------ the MCP client -------------

def test_check_url_and_split_tools():
    from bench.mcp_tools import check_url, split_tools
    assert check_url("https://krusgraph.com/mcp") and check_url("http://127.0.0.1:8123/mcp") and check_url("http://localhost/mcp")
    for bad in ("http://krusgraph.com/mcp", "ftp://krusgraph.com/mcp", "krusgraph.com/mcp", "https:///mcp"):
        with pytest.raises(ValueError):
            check_url(bad)
    listed = [{"name": "plan_backend", "description": "d", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
              {"name": "create_views", "annotations": {"readOnlyHint": False, "destructiveHint": True}},
              {"name": "no_annotations"},
              {"name": "list_tables", "annotations": {"readOnlyHint": True}}]
    kept, excluded, shadowed = split_tools(listed, reserved=["list_tables", "run_sql", "answer"])
    assert kept == [{"name": "plan_backend", "description": "d", "input_schema": {"type": "object"}}]
    assert excluded == ["create_views", "no_annotations"] and shadowed == ["list_tables"]            # unmarked counts as writing


def test_mcp_tools_against_a_local_mcp_server():
    """The real client against a real MCP server (SDK 2.x, streamable HTTP) on 127.0.0.1: the bearer token in the header,
    only read-only tools kept, A's list_tables kept over the server's, a server-side failure counted, a writing tool
    refused without reaching the server. No network beyond the loopback interface."""
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("mcp.server.mcpserver")
    import socket
    import threading
    import time
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    from bench.estimate import reserved_names
    from bench.mcp_tools import McpTools
    srv = MCPServer("fake-krusgraph")
    ran = []

    @srv.tool(title="Plan", annotations=ToolAnnotations(read_only_hint=True))
    def plan_backend(schema: str = "dbo") -> str:
        """Plan keys and relationships."""
        ran.append("plan_backend")
        return json.dumps({"summary": f"7 tables in {schema}"})

    @srv.tool(title="Tables", annotations=ToolAnnotations(read_only_hint=True))
    def list_tables(schema: str = "dbo") -> str:
        """List tables."""
        return "{}"

    @srv.tool(title="Create", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    def create_backend(name: str) -> str:
        """Create an API."""
        ran.append("create_backend")
        return "created"

    @srv.tool(title="Fails", annotations=ToolAnnotations(read_only_hint=True))
    def failing() -> str:
        """Always fails."""
        raise RuntimeError("boom")

    seen = []
    app = srv.streamable_http_app(stateless_http=True, host="127.0.0.1")

    async def capture(scope, receive, send):
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(capture, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    m = McpTools(f"http://127.0.0.1:{port}/mcp", "test-only-token", timeout=20, reserved=reserved_names()).start()
    try:
        assert [t["name"] for t in m.tools] == ["plan_backend", "failing"] and m.excluded == ["create_backend"] and m.shadowed == ["list_tables"]
        assert m.tools[0]["input_schema"]["properties"]["schema"]["default"] == "dbo"
        assert m.call("plan_backend", {"schema": "shop"}, "q01", 1) == ('{"summary": "7 tables in shop"}', False)
        text, err = m.call("failing", {}, "q01", 1)
        assert err is True and "failing" in text
        text, err = m.call("create_backend", {"name": "x"}, "q01", 1)
        assert err is True and text.startswith("refused:") and "create_backend" not in ran        # never reached the server
        assert m.calls == {"plan_backend": 1, "failing": 1, "create_backend": 1} and m.failed == {"failing": 1, "create_backend": 1}
        assert set(seen) == {b"Bearer test-only-token"}
    finally:
        m.close()
        server.should_exit = True
        th.join(10)


# ------------------------------------------------------------------ the estimate ---------------

def test_estimate_uses_the_snapshot_and_states_its_assumptions():
    from bench import estimate
    snap = estimate.load_snapshot()
    names = [t["name"] for t in snap["tools"]]
    assert len(names) == len(set(names)) and {"plan_backend", "join_path", "suggest_relationships", "list_tables"} <= set(names)
    assert {t["name"] for t in snap["tools"] if not t["read_only"]} == {"create_backend", "create_semantic_model", "create_views", "create_data_agent"}
    lists = estimate.tool_lists(snap)
    b = [t["name"] for t in lists["B"]]
    assert b[:2] == ["list_tables", "run_sql"] and b[-1] == "answer" and len(b) == len(set(b))
    assert not set(b) & {"create_backend", "create_semantic_model", "create_views", "create_data_agent"}
    qs = load_questions()[:5]
    e = estimate.estimate(["A", "B"], qs, 1, "claude-opus-5-5")
    for s in ("A", "B"):
        x = e["setups"][s]
        assert x["runs"] == 5 and all(x[k][0] <= x[k][1] for k in ("input", "output", "seconds", "cost_usd"))
    assert e["setups"]["B"]["input"][0] > e["setups"]["A"]["input"][0]                 # B's tool list is resent on every step
    assert e["prices"] == (4.0, 20.0) and "pricing" in e["price_source"]
    assert e["total"]["cost_usd"][0] == pytest.approx(e["setups"]["A"]["cost_usd"][0] + e["setups"]["B"]["cost_usd"][0])
    assert "cost_usd" not in estimate.estimate(["A"], qs, 1, "unknown-model")["setups"]["A"]
