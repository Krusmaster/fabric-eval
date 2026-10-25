"""Run the rig: python -m bench.run --setup A|B --model <model id> [--repeats 3] [--out bench/results]

Environment (never a file in the repo):
  ANTHROPIC_API_KEY        the Anthropic key
  FABRIC_SQL_SERVER        the lakehouse's SQL analytics endpoint host (…datawarehouse.fabric.microsoft.com)
  FABRIC_SQL_DATABASE      the lakehouse name (default demo_lh)
  FABRIC_SQL_TOKEN         an Entra access token for https://database.windows.net/
  FABRIC_SCHEMA            schema with the shop tables (default shop)
  KRUSGRAPH_MCP_URL        setup B only (default https://krusgraph.com/mcp)
  KRUSGRAPH_TOKEN          setup B only: the user's bearer token for Krusgraph
Results: bench/results/<date>-<setup>.jsonl, one line per question and repeat, judged as they complete."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .agent import run_question, verdict_for
from .tools import SqlTools, sqlserver_execute
from .truth import load_questions

HERE = Path(__file__).resolve().parent


def write_record(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def run(client, setup: str, model: str, toolsets: list, questions, repeats: int, out: Path, run_id: str,
        temperature: float = 0.0, max_steps: int = 12, progress=print) -> list[dict]:
    """The loop the CLI and the tests share: every question, in order, `repeats` times per question."""
    records = []
    for repeat in range(1, repeats + 1):
        for q in questions:
            rec = run_question(client, model, q, toolsets, run_id, setup, repeat, temperature, max_steps)
            rec.update(verdict_for(q, rec))
            write_record(out, rec)
            records.append(rec)
            progress(f"{setup} {q.id} r{repeat}: {rec['verdict']} steps={rec['steps']} tokens={rec['tokens_total']} sql={rec['sql_statements']} ({rec['sql_failed']} failed) {rec['seconds']}s")
    return records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", choices=["A", "B"], required=True)
    ap.add_argument("--model", required=True, help="Anthropic model id")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--questions", default=str(HERE / "questions.yaml"))
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--run-id", default=None, help="default <date>-<setup>")
    ap.add_argument("--only", default=None, help="comma-separated question ids, for a smoke run")
    a = ap.parse_args(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    missing = [k for k in ("FABRIC_SQL_SERVER", "FABRIC_SQL_TOKEN") if not os.environ.get(k)]
    if a.setup == "B" and not os.environ.get("KRUSGRAPH_TOKEN"):
        missing.append("KRUSGRAPH_TOKEN")
    if missing:
        print("missing environment: " + ", ".join(missing), file=sys.stderr)
        return 2
    import anthropic
    client = anthropic.Anthropic()                                      # reads ANTHROPIC_API_KEY itself
    run_id = a.run_id or f"{date.today().isoformat()}-{a.setup}"
    questions = load_questions(a.questions)
    if a.only:
        wanted = set(a.only.split(","))
        questions = [q for q in questions if q.id in wanted]
    execute = sqlserver_execute(os.environ["FABRIC_SQL_SERVER"], os.environ.get("FABRIC_SQL_DATABASE", "demo_lh"), os.environ["FABRIC_SQL_TOKEN"])
    toolsets = [SqlTools(execute, run_id)]
    mcp = None
    if a.setup == "B":
        from .mcp_tools import McpTools
        mcp = McpTools(os.environ.get("KRUSGRAPH_MCP_URL", "https://krusgraph.com/mcp"), os.environ["KRUSGRAPH_TOKEN"]).start()
        toolsets.append(mcp)
        print(f"Krusgraph tools: {', '.join(t['name'] for t in mcp.tools)}")
    out = Path(a.out) / f"{run_id}.jsonl"
    try:
        records = run(client, a.setup, a.model, toolsets, questions, a.repeats, out, run_id, a.temperature, a.max_steps)
    finally:
        getattr(execute, "close", lambda: None)()
        if mcp:
            mcp.close()
    ok = sum(1 for r in records if r["verdict"] == "correct")
    print(f"{len(records)} runs, {ok} correct -> {out}")
    print(f"Fabric-side metrics: python -m bench.fabric_metrics --results {out}   (wait up to 15 minutes for queryinsights)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
