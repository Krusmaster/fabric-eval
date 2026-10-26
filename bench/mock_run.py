"""Run the whole rig against the mocks: every question, both setups, three repeats, then build REPORT.md.

    python -m bench.mock_run [--out bench/results] [--repeats 3]

The fake model replays a scripted transcript per question (list the tables, run one or two SQL statements, answer),
with the answer taken from bench/truth.py, so most verdicts are correct by construction; a few are scripted wrong or
refused so the report has something to show. Token counts are constants (B's larger because its tool list is longer),
SQL runs on SQLite, Krusgraph tools are canned. The files are named …-mock-A/B and the report says so: this exercises
the pipeline, it measures nothing."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .mcp_tools import FakeMcpTools
from .mocks import FakeModel, block, krusgraph_fakes, seed_execute, use
from .run import run
from .tools import SqlTools
from .truth import answers, load_questions, through_customer

HERE = Path(__file__).resolve().parent

# one SQL per question that SQLite accepts and that an agent could plausibly write (COUNT_BIG/TOP avoided on purpose)
SQL = {
    "q01": "SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.name = 'Harbour Bikes'",
    "q02": "SELECT p.name FROM shop.products p JOIN shop.categories c ON p.category_id = c.id WHERE c.name = 'Tea'",
    "q03": "SELECT SUM(i.quantity) AS q FROM shop.order_items i JOIN shop.products p ON i.product_id = p.product_id WHERE p.name = 'Trail bike'",
    "q04": "SELECT c.name, COUNT(*) AS n FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id GROUP BY c.name ORDER BY n DESC",
    "q05": "SELECT DISTINCT a.city FROM shop.addresses a JOIN shop.customers c ON a.customer_id = c.customer_id WHERE c.name = 'Harbour Bikes'",
    "q06": "SELECT COUNT(*) AS n FROM shop.audit_log l JOIN shop.customers c ON l.customer_id = c.customer_id WHERE c.country = 'SE'",
    "q07": "SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.customers c ON o.customer_id = c.customer_id WHERE c.country = 'DE' AND o.order_date >= '2026-10-01' AND o.order_date < '2026-11-01'",
    "q08": "SELECT c.name, COUNT(*) AS n FROM shop.products p JOIN shop.categories c ON p.category_id = c.id GROUP BY c.name ORDER BY n DESC",
    "q09": "SELECT DISTINCT c.name FROM shop.customers c JOIN shop.orders o ON o.customer_id = c.customer_id JOIN shop.order_items i ON i.order_id = o.order_id JOIN shop.products p ON p.product_id = i.product_id JOIN shop.categories k ON k.id = p.category_id WHERE c.country = 'DE' AND k.name = 'Bikes'",
    "q10": "SELECT k.name, SUM(i.quantity) AS q FROM shop.order_items i JOIN shop.products p ON p.product_id = i.product_id JOIN shop.categories k ON k.id = p.category_id GROUP BY k.name ORDER BY q DESC",
    "q11": "SELECT COUNT(DISTINCT o.customer_id) AS n FROM shop.order_items i JOIN shop.products p ON p.product_id = i.product_id JOIN shop.orders o ON o.order_id = i.order_id WHERE p.name = 'Helmet'",
    "q12": "SELECT COUNT(*) AS n FROM shop.order_items i JOIN shop.orders o ON o.order_id = i.order_id JOIN shop.customers c ON c.customer_id = o.customer_id WHERE c.country = 'SE'",
    "q13": "SELECT DISTINCT p.name FROM shop.order_items i JOIN shop.products p ON p.product_id = i.product_id JOIN shop.orders o ON o.order_id = i.order_id JOIN shop.customers c ON c.customer_id = o.customer_id WHERE c.name = 'Fjord Outdoor'",
    "q14": "SELECT c.country, SUM(i.quantity) AS q FROM shop.order_items i JOIN shop.products p ON p.product_id = i.product_id JOIN shop.categories k ON k.id = p.category_id JOIN shop.orders o ON o.order_id = i.order_id JOIN shop.customers c ON c.customer_id = o.customer_id WHERE k.name = 'Tea' GROUP BY c.country ORDER BY q DESC",
    "q15": "SELECT COUNT(*) AS n FROM (SELECT i.order_id FROM shop.order_items i JOIN shop.products p ON p.product_id = i.product_id GROUP BY i.order_id HAVING COUNT(DISTINCT p.category_id) > 1)",
    "q16": "SELECT c.name, SUM(i.quantity) AS q FROM shop.order_items i JOIN shop.orders o ON o.order_id = i.order_id JOIN shop.customers c ON c.customer_id = o.customer_id GROUP BY c.name ORDER BY q DESC",
    "q17": "SELECT a.city FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id WHERE o.order_id = 103",
    "q18": "SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id WHERE a.city = 'Hamburg'",
    "q19": "SELECT quantity FROM shop.order_items WHERE order_id = 105 AND line_no = 2",
    "q20": "SELECT DISTINCT c.name FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id JOIN shop.customers c ON c.customer_id = o.customer_id WHERE a.city = 'Leipzig'",
    "q21": "SELECT DISTINCT p.name FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id JOIN shop.order_items i ON i.order_id = o.order_id JOIN shop.products p ON p.product_id = i.product_id WHERE a.city = 'Uppsala'",
    "q22": "SELECT COUNT(*) AS n FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id WHERE a.customer_id <> o.customer_id",
    "q23": "SELECT SUM(i.quantity) AS q FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id JOIN shop.order_items i ON i.order_id = o.order_id JOIN shop.products p ON p.product_id = i.product_id JOIN shop.categories k ON k.id = p.category_id WHERE a.city = 'Leipzig' AND k.name = 'Tea'",
    "q24": "SELECT a.city, COUNT(*) AS n FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id GROUP BY a.city HAVING COUNT(*) < 5",
    "q25": "SELECT COUNT(DISTINCT i.product_id) AS n FROM shop.orders o JOIN shop.addresses a ON a.address_id = o.ship_to_id JOIN shop.order_items i ON i.order_id = o.order_id WHERE a.city = 'Kiel'",
}
SHIP_TO = {"source": "orders", "source_cols": ["ship_to_id"], "target": "addresses", "target_cols": ["address_id"]}
# scripted imperfections, so the report is not all green: (question, repeat) -> what the fake does instead
A_WRONG = {("q17", 1): "Hamburg", ("q20", 1): ["Harbour Bikes"], ("q18", 2): 20,      # A guesses the ship_to link through the customer's own address
           ("q21", 1): through_customer("q21"), ("q24", 1): through_customer("q24")}   # ...and joins addresses through the customer
A_REFUSED = {("q19", 1)}                                                               # A does not find the composite key once
A_FAILED_FIRST = {"q09", "q14", "q15"}                                                 # a syntax error on the first attempt, then the right SQL
B_WRONG = {("q15", 3): 40}
B_FAILED_GRAPHQL = {"q01", "q09"}                                                      # B tries query_graphql, which has no API, then falls back to SQL


def scripts_for(setup: str, questions, repeat: int) -> dict:
    truth = answers()
    out = {}
    for q in questions:
        source = q.source
        turns = []
        if repeat == 1:
            turns.append([use("list_tables", schema="shop")])
        if setup == "B":
            turns.append([use("plan_backend", workspace="demo", source="demo_lh", schema="shop")])
            if q.id in B_FAILED_GRAPHQL:
                turns.append([use("query_graphql", workspace="demo", api="shop_api", query="{ customers(first: 5) { items { name } } }")])
            if "ship_to" in q.tags:
                turns.append([use("suggest_relationships", workspace="demo", source="demo_lh", schema="shop")])
            if "join_path" in q.tags:
                turns.append([use("join_path", workspace="demo", source="demo_lh", schema="shop", start="orders", end="addresses", confirmed=[SHIP_TO])])
        if setup == "A" and q.id in A_FAILED_FIRST and repeat == 1:
            turns.append([use("run_sql", query=SQL[q.id].replace("JOIN", "JOIM", 1))])
        turns.append([use("run_sql", query=SQL[q.id])])
        key = (q.id, repeat)
        if setup == "A" and key in A_REFUSED:
            turns.append([use("answer", value=None, source=source, refused=True, note="could not identify the line key")])
        elif setup == "A" and key in A_WRONG:
            turns.append([use("answer", value=A_WRONG[key], source=source, refused=False)])
        elif setup == "B" and key in B_WRONG:
            turns.append([use("answer", value=B_WRONG[key], source=source, refused=False)])
        else:
            turns.append([block(type="text", text="Computed from the query."), use("answer", value=truth[q.id], source=source, refused=False)])
        out[q.question] = turns
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.mock_run")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--date", default=None)
    ap.add_argument("--report", default=str(HERE / "REPORT.md"))
    a = ap.parse_args(argv)
    from datetime import date
    day = a.date or date.today().isoformat()
    questions = load_questions()
    written = []
    for setup, usage in (("A", (1800, 70, 0, 0)), ("B", (4200, 70, 0, 0))):
        run_id = f"{day}-mock-{setup}"
        out = Path(a.out) / f"{run_id}.jsonl"
        if out.exists():
            out.unlink()
        execute = seed_execute()
        toolsets = [SqlTools(execute, run_id)] + ([FakeMcpTools(krusgraph_fakes())] if setup == "B" else [])
        for repeat in range(1, a.repeats + 1):
            use.n = 0
            model = FakeModel(scripts_for(setup, questions, repeat), usage=usage if repeat == 1 else (usage[0] // 2, usage[1], usage[0] // 2, 0))
            run(model, setup, "mock-model (replayed transcript, no API call)", toolsets, questions, 1, out, run_id, progress=lambda s: None)
            # run() numbers repeats itself from 1; renumber this pass's lines so repeats stay distinct in the file
            _renumber(out, repeat)
        execute.close()
        written.append(out)
        print(f"{setup}: {out}")
    from .report import main as report_main
    report_main([str(p) for p in written] + ["--out", a.report])
    return 0


def _renumber(path: Path, repeat: int) -> None:
    import json
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        rec = json.loads(line)
        if rec.get("_pass") is None:
            rec["repeat"] = repeat
            rec["_pass"] = repeat
        out.append(json.dumps(rec, default=str))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
