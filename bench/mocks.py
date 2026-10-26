"""Stand-ins for the parts that would touch the outside world: a SQLite copy of the shop lakehouse, a replaying fake
model with synthetic usage numbers, and fake Krusgraph tools. Used by the tests and by `python -m bench.mock_run`.
Nothing here is a measurement: token counts are invented constants, SQL CPU time does not exist in SQLite."""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
from types import SimpleNamespace


def seed_execute():
    """A SQLite database with the seed rows under schema `shop`; INFORMATION_SCHEMA.COLUMNS answered from PRAGMA."""
    from fabric_backend import seed
    con = sqlite3.connect(":memory:")
    con.execute("ATTACH DATABASE ':memory:' AS shop")
    for name, ddl in seed.SCHEMAS.items():
        con.execute(f"CREATE TABLE shop.[{name}] ({ddl})")
        data = [tuple(v.isoformat() if isinstance(v, (datetime.date, datetime.datetime)) else v for v in r) for r in seed.rows()[name]]
        con.executemany(f"INSERT INTO shop.[{name}] VALUES ({','.join('?' * len(data[0]))})", data)
    seen: list[str] = []

    def execute(sql):
        seen.append(sql)
        body = re.sub(r"/\* bench:[^*]* \*/ ", "", sql)
        if "INFORMATION_SCHEMA.COLUMNS" in body:
            rows = []
            for name in seed.SCHEMAS:
                for c in con.execute(f"PRAGMA shop.table_info([{name}])").fetchall():
                    rows.append({"TABLE_NAME": name, "COLUMN_NAME": c[1], "DATA_TYPE": c[2].lower()})
            return rows
        cur = con.execute(body)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    execute.seen = seen
    execute.close = con.close
    return execute


def block(**kw):
    return SimpleNamespace(**kw)


def use(name, **inp):
    use.n += 1
    return block(type="tool_use", id=f"tu{use.n}", name=name, input=inp)


use.n = 0


class FakeModel:
    """Replays scripted turns per question: scripts[question text] = [[blocks], [blocks], …]; each create() consumes one.
    `usage` is (input, output, cache_read, cache_creation) per step — invented numbers, the same every step."""

    def __init__(self, scripts: dict, usage=(1000, 50, 0, 0)):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []
        self.usage = usage
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        q = kw["messages"][0]["content"]
        turns = self.scripts.get(q, [])
        blocks = turns.pop(0) if turns else [block(type="text", text="I give up")]
        i, o, cr, cc = self.usage
        return SimpleNamespace(content=blocks, stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn",
                               usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cc))


def krusgraph_fakes() -> dict:
    """A handful of Krusgraph tools with canned answers (the shapes the real server gives), for setup B scripts."""
    plan = {"summary": "7 tables, 6 with keys, 6 relationships", "relationships": [
        {"source": "orders", "source_cols": ["customer_id"], "target": "customers", "rule": "R1"},
        {"source": "order_items", "source_cols": ["order_id"], "target": "orders", "rule": "R1"},
        {"source": "order_items", "source_cols": ["product_id"], "target": "products", "rule": "R1"},
        {"source": "products", "source_cols": ["category_id"], "target": "categories", "rule": "R1"},
        {"source": "addresses", "source_cols": ["customer_id"], "target": "customers", "rule": "R1"},
        {"source": "audit_log", "source_cols": ["customer_id"], "target": "customers", "rule": "R1"}], "keyless": ["audit_log"]}
    return {
        "plan_backend": lambda a: json.dumps(plan),
        "join_path": lambda a: json.dumps({"start": a.get("start"), "end": a.get("end"), "paths": [{"hops": 2, "steps": []}]}),
        "suggest_relationships": lambda a: json.dumps({"candidates": [{"source": "orders", "source_cols": ["ship_to_id"], "target": "addresses",
                                                                        "target_cols": ["address_id"], "evidence": {"inclusion": 1.0}}]}),
        "query_graphql": lambda a: json.dumps({"error": "no API for GraphQL named 'shop_api'", "kind": "not_found", "hint": "create_backend creates one"}),
    }
