"""Run the rig: python -m bench.run --setups A|B|A,B [--model claude-opus-5-5] [--repeats 3] [--limit N | --only q01,q17]
[--dry-run]

--dry-run prints exactly which questions and setups would run, in which order, with which tools, which environment
variables are missing, and a cost and time estimate (labelled as one, see bench/estimate.py). It calls nothing.

A real run first checks that the lakehouse holds exactly the rows the answers are computed from (truth.check_lakehouse,
one SELECT per table) and stops before any model call if it does not; --skip-seed-check leaves that out.

Environment (never a file in the repo):
  ANTHROPIC_API_KEY        the Anthropic key
  FABRIC_SQL_SERVER        the lakehouse's SQL analytics endpoint host (…datawarehouse.fabric.microsoft.com)
  FABRIC_SQL_DATABASE      the lakehouse name (default demo_lh)
  FABRIC_SQL_TOKEN         an Entra access token for https://database.windows.net/
  FABRIC_SCHEMA            schema with the shop tables (default shop)
  KRUSGRAPH_MCP_URL        setup B only (default https://krusgraph.com/mcp; must be https)
  KRUSGRAPH_TOKEN          setup B only: the user's bearer token for Krusgraph
Results: bench/results/<date>-<setup>.jsonl, one line per question and repeat, judged as they complete. With two setups
they run one after the other, A first, each into its own file."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .agent import DEFAULT_MAX_TOKENS, run_question, verdict_for
from .estimate import DEFAULT_MODEL
from .mcp_tools import DEFAULT_URL
from .truth import load_questions

HERE = Path(__file__).resolve().parent
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def write_record(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def run(client, setup: str, model: str, toolsets: list, questions, repeats: int, out: Path, run_id: str,
        temperature: float | None = None, max_steps: int = 12, progress=print, max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = None) -> list[dict]:
    """The loop the CLI and the tests share: every question, in order, `repeats` times per question."""
    records = []
    for repeat in range(1, repeats + 1):
        for q in questions:
            rec = run_question(client, model, q, toolsets, run_id, setup, repeat, temperature, max_steps, max_tokens, effort=effort)
            rec.update(verdict_for(q, rec))
            write_record(out, rec)
            records.append(rec)
            progress(f"{setup} {q.id} r{repeat}: {rec['verdict']} steps={rec['steps']} tokens={rec['tokens_total']} sql={rec['sql_statements']} ({rec['sql_failed']} failed) {rec['seconds']}s")
    return records


def setups_from(a) -> list[str]:
    raw = a.setups or a.setup
    if not raw:
        raise SystemExit("give --setups A, B or A,B")
    out = [s.strip().upper() for s in raw.split(",") if s.strip()]
    bad = [s for s in out if s not in ("A", "B")]
    if bad or not out:
        raise SystemExit(f"--setups takes A, B or A,B (got {raw!r})")
    return sorted(set(out))                                             # A before B, each once


def select_questions(questions, only: str | None, limit: int | None):
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        known = {q.id for q in questions}
        missing = [x for x in wanted if x not in known]
        if missing:
            raise SystemExit(f"--only: no such question: {', '.join(missing)}")
        questions = [q for q in questions if q.id in set(wanted)]
    if limit is not None:
        if limit < 1:
            raise SystemExit("--limit must be 1 or more")
        questions = questions[:limit]
    return questions


def environment(setups: list[str]) -> dict[str, bool]:
    names = ["ANTHROPIC_API_KEY", "FABRIC_SQL_SERVER", "FABRIC_SQL_TOKEN"] + (["KRUSGRAPH_TOKEN"] if "B" in setups else [])
    return {k: bool(os.environ.get(k)) for k in names}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setups", default=None, help="A, B or A,B (run one after the other, A first)")
    ap.add_argument("--setup", choices=["A", "B"], default=None, help="one setup (same as --setups A or --setups B)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model id (default {DEFAULT_MODEL}); the same for A and B")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions in file order")
    ap.add_argument("--only", default=None, help="comma-separated question ids")
    ap.add_argument("--effort", choices=EFFORTS, default=None, help="output_config.effort; default: the model's own")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="per response (thinking counts toward it)")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--questions", default=str(HERE / "questions.yaml"))
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--run-id", default=None, help="default <date>-<setup>; with two setups, <run-id>-<setup>")
    ap.add_argument("--dry-run", action="store_true", help="print what would run and an estimate; call nothing")
    ap.add_argument("--price-in", type=float, default=None, help="USD per million input tokens, for the estimate")
    ap.add_argument("--price-out", type=float, default=None, help="USD per million output tokens, for the estimate")
    ap.add_argument("--skip-seed-check", action="store_true", help="do not compare the lakehouse with fabric_backend.seed first")
    a = ap.parse_args(argv)
    setups = setups_from(a)
    if a.repeats < 1:
        raise SystemExit("--repeats must be 1 or more")
    questions = select_questions(load_questions(a.questions), a.only, a.limit)
    day = date.today().isoformat()
    run_ids = {s: (a.run_id if a.run_id and len(setups) == 1 else f"{a.run_id}-{s}" if a.run_id else f"{day}-{s}") for s in setups}
    url = os.environ.get("KRUSGRAPH_MCP_URL", DEFAULT_URL)
    env = environment(setups)

    if a.dry_run:
        from .estimate import estimate, plan_text
        est = estimate(setups, questions, a.repeats, a.model, a.price_in, a.price_out)
        print(plan_text(setups, questions, a.repeats, a.model, env, Path(a.out), run_ids, a.effort, a.max_tokens, url, est,
                        seed_check=not a.skip_seed_check), end="")
        return 0

    missing = [k for k, ok in env.items() if not ok]
    if missing:
        print("missing environment: " + ", ".join(missing), file=sys.stderr)
        return 2
    if "B" in setups:
        from .mcp_tools import check_url
        try:
            check_url(url)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    import anthropic
    from .tools import SqlTools, sqlserver_execute
    if not a.skip_seed_check:
        from .truth import check_lakehouse
        schema = os.environ.get("FABRIC_SCHEMA", "shop")
        execute = sqlserver_execute(os.environ["FABRIC_SQL_SERVER"], os.environ.get("FABRIC_SQL_DATABASE", "demo_lh"), os.environ["FABRIC_SQL_TOKEN"])
        try:
            problems = check_lakehouse(execute, schema)
        finally:
            getattr(execute, "close", lambda: None)()
        if problems:
            print("the lakehouse does not hold the rows the answers are computed from (fabric_backend.seed); nothing was run:",
                  file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            print("re-seed it with the fabric-backend commit pinned in pyproject.toml (bench/README.md, Before), "
                  "or pass --skip-seed-check", file=sys.stderr)
            return 2
        print(f"seed check: every table in {schema} holds exactly the seed rows")
    client = anthropic.Anthropic()                                      # reads ANTHROPIC_API_KEY itself
    for setup in setups:
        run_id = run_ids[setup]
        execute = sqlserver_execute(os.environ["FABRIC_SQL_SERVER"], os.environ.get("FABRIC_SQL_DATABASE", "demo_lh"), os.environ["FABRIC_SQL_TOKEN"])
        toolsets = [SqlTools(execute, run_id)]
        mcp = None
        if setup == "B":
            from .estimate import reserved_names
            from .mcp_tools import McpTools
            mcp = McpTools(url, os.environ["KRUSGRAPH_TOKEN"], reserved=reserved_names()).start()
            toolsets.append(mcp)
            print(f"Krusgraph tools (read-only): {', '.join(t['name'] for t in mcp.tools)}")
            print(f"left out (they write): {', '.join(mcp.excluded) or 'none'}; "
                  f"left out (setup A's tool of the same name is kept): {', '.join(mcp.shadowed) or 'none'}")
        out = Path(a.out) / f"{run_id}.jsonl"
        try:
            records = run(client, setup, a.model, toolsets, questions, a.repeats, out, run_id, None, a.max_steps,
                          max_tokens=a.max_tokens, effort=a.effort)
        finally:
            getattr(execute, "close", lambda: None)()
            if mcp:
                mcp.close()
        ok = sum(1 for r in records if r["verdict"] == "correct")
        print(f"{setup}: {len(records)} runs, {ok} correct -> {out}")
        print(f"Fabric-side metrics: python -m bench.fabric_metrics --results {out}   (wait up to 15 minutes for queryinsights)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
