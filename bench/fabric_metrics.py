"""Fabric-side cost per tagged statement, read afterwards from the lakehouse's queryinsights views.

Source (MicrosoftDocs/fabric-docs @6b1f955, docs/data-warehouse/query-insights.md, "ms.date: 08/24/2026", read 25 Oct 2026):
queryinsights views exist in the SQL analytics endpoint and in warehouses; `exec_requests_history` "returns
information about each completed SQL request/query"; completed queries "can take up to 15 minutes to appear".
Columns the page uses in its examples: distributed_statement_id, query_hash, allocated_cpu_time_ms, label, command,
data_scanned_remote_storage_mb, data_scanned_memory_mb, data_scanned_disk_mb, start_time, submit_time, login_name.
The full column list lives on learn.microsoft.com (queryinsights-exec-requests-history-transact-sql), which this
environment could not fetch, so the fetcher reads SELECT * and reports which of the columns it wanted are missing;
an elapsed-time column (total_elapsed_time_ms) is taken if present and reported as missing otherwise.
The page's own examples filter on `last_run_command LIKE '%<some_label>%'`, so the statement text is searchable;
the rig tags every statement with /* bench:<run_id>:<question_id>:r<repeat> */ and matches on that.

Krusgraph's own statements (setup B: list_tables, plan_backend, validate_plan, sample_rows … read the endpoint too)
carry no bench tag. They are counted separately as "untagged statements in the run's time window" and belong to B.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WANTED = ["allocated_cpu_time_ms", "data_scanned_remote_storage_mb", "data_scanned_memory_mb", "data_scanned_disk_mb",
          "total_elapsed_time_ms", "submit_time", "start_time", "command", "distributed_statement_id"]
TAG = re.compile(r"bench:([^:*\s]+):(q\d+):r(\d+)")


def tagged_sql(run_id: str) -> str:
    lit = run_id.replace("'", "''")
    return f"SELECT * FROM queryinsights.exec_requests_history WHERE command LIKE '%bench:{lit}:%'"


def window_sql(start_iso: str, end_iso: str) -> str:
    return ("SELECT * FROM queryinsights.exec_requests_history "
            f"WHERE submit_time >= '{start_iso}' AND submit_time <= '{end_iso}' AND command NOT LIKE '%bench:%'")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def summarise(rows: list[dict]) -> dict:
    """Per (question_id, repeat): statement count, CPU ms and data scanned (MB), summed; plus which columns were missing."""
    rows = [{str(k).lower(): v for k, v in r.items()} for r in rows]
    present = set(rows[0].keys()) if rows else set()
    missing = [c for c in WANTED if c not in present] if rows else WANTED[:]
    by: dict[str, dict] = {}
    for r in rows:
        m = TAG.search(str(r.get("command", "")))
        key = f"{m.group(2)}:r{m.group(3)}" if m else "untagged"
        e = by.setdefault(key, {"statements": 0, "cpu_ms": 0.0, "data_scanned_remote_mb": 0.0, "data_scanned_memory_mb": 0.0,
                                "data_scanned_disk_mb": 0.0, "elapsed_ms": 0.0})
        e["statements"] += 1
        e["cpu_ms"] += _num(r.get("allocated_cpu_time_ms"))
        e["data_scanned_remote_mb"] += _num(r.get("data_scanned_remote_storage_mb"))
        e["data_scanned_memory_mb"] += _num(r.get("data_scanned_memory_mb"))
        e["data_scanned_disk_mb"] += _num(r.get("data_scanned_disk_mb"))
        e["elapsed_ms"] += _num(r.get("total_elapsed_time_ms"))
    for e in by.values():
        for k in list(e):
            if isinstance(e[k], float):
                e[k] = round(e[k], 3)
    return {"by_run": by, "columns_missing": missing, "rows": len(rows)}


def fetch(execute, run_id: str, window: tuple[str, str] | None = None) -> dict:
    """execute(sql) -> rows. Reads the tagged statements of a run and, when a (start, end) window is given, the
    untagged statements in it (Krusgraph's own, in setup B)."""
    out = summarise(list(execute(tagged_sql(run_id))))
    if window:
        other = summarise(list(execute(window_sql(*window))))
        out["untagged_in_window"] = other["by_run"].get("untagged", {"statements": 0})
        out["untagged_note"] = ("statements in the run's window without a bench tag: Krusgraph's own reads of the endpoint "
                                "(setup B) and anything else that ran then; attribute them to B only if nothing else used the endpoint")
    return out


def attach(results_path: Path, fetched: dict) -> int:
    """Add a `fabric` field to each record of the results file, matched by question_id and repeat. Returns matches."""
    lines = results_path.read_text(encoding="utf-8").splitlines()
    out, hits = [], 0
    for line in lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        key = f"{rec['question_id']}:r{rec['repeat']}"
        f = fetched["by_run"].get(key)
        if f:
            rec["fabric"] = {**f, "columns_missing": fetched["columns_missing"]}
            hits += 1
        else:
            rec["fabric"] = {"statements": 0, "note": "no tagged statement found in queryinsights (yet)"}
        out.append(json.dumps(rec, default=str))
    results_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return hits


def main(argv=None) -> int:
    import os
    from .tools import sqlserver_execute
    ap = argparse.ArgumentParser(prog="python -m bench.fabric_metrics")
    ap.add_argument("--results", required=True, help="a bench/results/<run>.jsonl file")
    ap.add_argument("--run-id", default=None, help="default: the file's stem")
    ap.add_argument("--window", nargs=2, metavar=("START", "END"), help="ISO timestamps (UTC) to count untagged statements in")
    a = ap.parse_args(argv)
    path = Path(a.results)
    run_id = a.run_id or path.stem
    missing = [k for k in ("FABRIC_SQL_SERVER", "FABRIC_SQL_TOKEN") if not os.environ.get(k)]
    if missing:
        print("missing environment: " + ", ".join(missing), file=sys.stderr)
        return 2
    execute = sqlserver_execute(os.environ["FABRIC_SQL_SERVER"], os.environ.get("FABRIC_SQL_DATABASE", "demo_lh"), os.environ["FABRIC_SQL_TOKEN"])
    try:
        fetched = fetch(execute, run_id, tuple(a.window) if a.window else None)
    finally:
        execute.close()
    hits = attach(path, fetched)
    print(f"{fetched['rows']} tagged statements, {hits} runs matched; columns missing: {fetched['columns_missing'] or 'none'}")
    if "untagged_in_window" in fetched:
        print(f"untagged in window: {fetched['untagged_in_window']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
