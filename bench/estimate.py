"""What a run would do and roughly cost, without calling anything: the plan behind `python -m bench.run --dry-run`.

Every number here is an ESTIMATE from the assumptions in ASSUMPTIONS, printed with them; the JSONL records of a real
run hold the measured numbers. Nothing is sent anywhere: no model, no SQL endpoint, no Krusgraph.

Input tokens grow inside a run: every step resends the tool definitions, the system prompt and the whole transcript so
far (the agent sets no prompt caching, so all of it is billed as input). So per run

    input  = sum over steps k of (fixed + (k - 1) x growth)     fixed  = tools + system + question
    output = steps x output per step                             growth = output per step + tool result per step

Tokens from characters: chars / 4 for the low end, x 1.35 for the high end (the tokenizer of current models uses up to
1.35 times as many tokens for the same text). The API's tool-use preamble is not modelled. For exact figures before a
run, the Messages API's count_tokens endpoint takes the same model, system, tools and first message.

Setup B's tool list comes from bench/krusgraph_tools.json, a snapshot of Krusgraph's tools/list (regenerate with
`python -m bench.estimate --snapshot` where fabric-backend's server extra is installed); a real run fetches the live list
at start and keeps only the read-only tools, as here."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "krusgraph_tools.json"
DEFAULT_MODEL = "claude-opus-5-5"

# USD per million tokens (input, output), Anthropic first-party list prices. Prompt caching is not used by the agent, so
# cache prices do not enter the estimate.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),          # prompts up to 100K tokens
}
PRICE_SOURCE = ("Anthropic's model table as of 6 October 2026; check https://platform.claude.com/docs/en/about-claude/pricing "
                "before relying on it, or pass --price-in/--price-out")

# (low, high) per run step, by setup. Judgement, not measurement: replace with the medians of a first real run.
ASSUMPTIONS = {
    "steps": {"A": (3, 6), "B": (4, 8)},                                  # model calls per question
    "tool_result_chars": {"A": (300, 3000), "B": (600, 6000)},            # added to the transcript per step
    "output_tokens_per_step": (150, 1500),                                # text, tool calls and thinking
    "seconds_per_step": (3, 15),                                          # model latency plus the tool call
    "chars_per_token": 4.0,
    "tokenizer_high_factor": 1.35,
}


def _tokens(chars: float) -> tuple[float, float]:
    low = chars / ASSUMPTIONS["chars_per_token"]
    return low, low * ASSUMPTIONS["tokenizer_high_factor"]


def load_snapshot(path: Path = SNAPSHOT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tool_lists(snapshot: dict | None = None) -> dict:
    """The tool definitions each setup's model would see (the `answer` tool included), and B's excluded tools."""
    from .agent import ANSWER_TOOL
    from .mcp_tools import split_tools
    from .tools import TOOLS
    snap = snapshot if snapshot is not None else load_snapshot()
    listed = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {}),
               "annotations": {"readOnlyHint": bool(t.get("read_only"))}} for t in snap["tools"]]
    kept, excluded, shadowed = split_tools(listed, reserved=reserved_names())
    return {"A": list(TOOLS) + [ANSWER_TOOL], "B": list(TOOLS) + kept + [ANSWER_TOOL], "B_excluded": excluded,
            "B_shadowed": shadowed, "snapshot_source": snap.get("source", "?"), "snapshot_date": snap.get("generated", "?")}


def reserved_names() -> list[str]:
    """The tool names setup A already has; Krusgraph tools of the same name are left out of B."""
    from .agent import ANSWER_TOOL
    from .tools import TOOLS
    return [t["name"] for t in TOOLS] + [ANSWER_TOOL["name"]]


def per_run(setup: str, tools: list, system: str, question: str) -> dict:
    """(low, high) input tokens, output tokens and seconds for one run of one question."""
    fixed_chars = len(json.dumps(tools)) + len(system) + len(question)
    s_lo, s_hi = ASSUMPTIONS["steps"][setup]
    r_lo, r_hi = ASSUMPTIONS["tool_result_chars"][setup]
    o_lo, o_hi = ASSUMPTIONS["output_tokens_per_step"]
    f_lo, f_hi = _tokens(fixed_chars)
    g_lo = _tokens(r_lo)[0] + o_lo
    g_hi = _tokens(r_hi)[1] + o_hi

    def total(steps, fixed, growth):
        return sum(fixed + k * growth for k in range(steps))
    sec_lo, sec_hi = ASSUMPTIONS["seconds_per_step"]
    return {"input": (total(s_lo, f_lo, g_lo), total(s_hi, f_hi, g_hi)), "output": (s_lo * o_lo, s_hi * o_hi),
            "seconds": (s_lo * sec_lo, s_hi * sec_hi), "tool_tokens": _tokens(len(json.dumps(tools)))}


def estimate(setups: list[str], questions: list, repeats: int, model: str, price_in: float | None = None,
             price_out: float | None = None, snapshot: dict | None = None) -> dict:
    from .agent import SYSTEM
    lists = tool_lists(snapshot)
    prices = (price_in, price_out) if price_in is not None and price_out is not None else PRICES.get(model)
    out = {"model": model, "repeats": repeats, "questions": len(questions), "setups": {}, "prices": prices,
           "price_source": "given on the command line" if price_in is not None and price_out is not None else
                           (PRICE_SOURCE if prices else f"no list price known for {model!r}: pass --price-in and --price-out")}
    totals = {"runs": 0, "input": [0.0, 0.0], "output": [0.0, 0.0], "seconds": [0.0, 0.0]}
    for s in setups:
        agg = {"runs": len(questions) * repeats, "input": [0.0, 0.0], "output": [0.0, 0.0], "seconds": [0.0, 0.0],
               "tools": [t["name"] for t in lists[s]], "tool_tokens": None}
        for q in questions:
            r = per_run(s, lists[s], SYSTEM, q.question)
            agg["tool_tokens"] = r["tool_tokens"]
            for k in ("input", "output", "seconds"):
                agg[k][0] += r[k][0] * repeats
                agg[k][1] += r[k][1] * repeats
        if prices:
            agg["cost_usd"] = [agg["input"][i] / 1e6 * prices[0] + agg["output"][i] / 1e6 * prices[1] for i in (0, 1)]
        out["setups"][s] = agg
        totals["runs"] += agg["runs"]
        for k in ("input", "output", "seconds"):
            totals[k][0] += agg[k][0]
            totals[k][1] += agg[k][1]
    if prices:
        totals["cost_usd"] = [sum(out["setups"][s]["cost_usd"][i] for s in setups) for i in (0, 1)]
    out["total"] = totals
    out["excluded_b"] = lists["B_excluded"]
    out["shadowed_b"] = lists["B_shadowed"]
    out["snapshot"] = f"{lists['snapshot_source']} ({lists['snapshot_date']})"
    return out


def _m(x: float) -> str:
    return f"{x / 1e6:.2f} M" if x >= 1e5 else f"{x / 1e3:.0f} k"


def _t(sec: float) -> str:
    return f"{sec / 3600:.1f} h" if sec >= 5400 else f"{sec / 60:.0f} min"


def plan_text(setups: list[str], questions: list, repeats: int, model: str, env: dict[str, bool], out_dir: Path,
              run_ids: dict[str, str], effort: str | None, max_tokens: int, url: str, est: dict) -> str:
    """The dry-run report: exactly what would run, in which order, with which tools, and the estimate."""
    lines = ["DRY RUN: nothing is called (no model, no SQL endpoint, no Krusgraph) and nothing is written.", "",
             f"Model: {model}   effort: {effort or 'model default'}   max_tokens per response: {max_tokens}   temperature: not sent",
             f"Setups, one after the other: {', '.join(setups)}   repeats: {repeats}   questions: {len(questions)}   "
             f"runs: {len(questions) * repeats * len(setups)}",
             "Order inside a setup: repeat 1 over every question in file order, then repeat 2, and so on.", ""]
    for s in setups:
        e = est["setups"][s]
        lines.append(f"Setup {s} -> {out_dir / (run_ids[s] + '.jsonl')}")
        if s == "A":
            lines.append("  tools: list_tables, run_sql (one T-SQL SELECT per call; anything else is refused before it reaches the endpoint), answer")
        else:
            kg = [n for n in e["tools"] if n not in ("list_tables", "run_sql", "answer")]
            lines.append(f"  tools: list_tables, run_sql, answer + {len(kg)} read-only Krusgraph tools from {url}:")
            lines.append("    " + ", ".join(kg))
            lines.append(f"  left out (they create items in Fabric): {', '.join(est['excluded_b']) or 'none'}")
            if est.get("shadowed_b"):
                lines.append(f"  left out (setup A's tool of the same name is kept): {', '.join(est['shadowed_b'])}")
            lines.append(f"  tool list from the snapshot {est['snapshot']}; the live list is fetched at start and filtered the same way")
        lines.append(f"  tool definitions: ~{e['tool_tokens'][0]:.0f}–{e['tool_tokens'][1]:.0f} tokens, resent on every step")
    lines += ["", "Questions:"]
    for q in questions:
        lines.append(f"  {q.id} [{', '.join(q.tags)}] {q.question}  (expected source: {q.source})")
    lines += ["", "Environment (values are never printed):"]
    for k, ok in env.items():
        lines.append(f"  {k}: {'set' if ok else 'MISSING'}")
    lines += ["", "ESTIMATE (assumptions, not a measurement):"]
    for s in setups:
        e = est["setups"][s]
        cost = f", ${e['cost_usd'][0]:.2f}–${e['cost_usd'][1]:.2f}" if "cost_usd" in e else ""
        lines.append(f"  {s}: {e['runs']} runs, input {_m(e['input'][0])}–{_m(e['input'][1])} tokens, output "
                     f"{_m(e['output'][0])}–{_m(e['output'][1])} tokens{cost}, {_t(e['seconds'][0])}–{_t(e['seconds'][1])}")
    t = est["total"]
    cost = f"${t['cost_usd'][0]:.2f}–${t['cost_usd'][1]:.2f}" if "cost_usd" in t else "not estimated"
    lines.append(f"  total: {t['runs']} runs, model cost {cost}, wall time {_t(t['seconds'][0])}–{_t(t['seconds'][1])} "
                 "(sequential; the capacity must run that long)")
    if est["prices"]:
        lines.append(f"  prices: ${est['prices'][0]:.2f} input / ${est['prices'][1]:.2f} output per million tokens; {est['price_source']}")
    else:
        lines.append(f"  prices: {est['price_source']}")
    a = ASSUMPTIONS
    lines.append(f"  assumptions per run: steps A {a['steps']['A'][0]}–{a['steps']['A'][1]}, B {a['steps']['B'][0]}–{a['steps']['B'][1]}; "
                 f"tool results per step A {a['tool_result_chars']['A'][0]}–{a['tool_result_chars']['A'][1]} chars, "
                 f"B {a['tool_result_chars']['B'][0]}–{a['tool_result_chars']['B'][1]}; output {a['output_tokens_per_step'][0]}–"
                 f"{a['output_tokens_per_step'][1]} tokens per step; {a['seconds_per_step'][0]}–{a['seconds_per_step'][1]} s per step; "
                 "no prompt caching; Fabric capacity usage not estimated")
    return "\n".join(lines) + "\n"


def snapshot(out: Path = SNAPSHOT, note: str = "") -> dict:
    """Write Krusgraph's tools/list as fabric-backend's server registers it (needs `fabric-backend[server]` installed).
    `note` says which commit it came from; the deployed service can be behind main."""
    import asyncio
    from fabric_backend import server
    from fabric_backend.discovery import package_version
    listed = asyncio.run(server.mcp.list_tools())
    tools = []
    for t in listed:
        d = t.model_dump(exclude_none=True, by_alias=True)
        tools.append({"name": d["name"], "description": d.get("description", ""), "input_schema": d.get("inputSchema", {}),
                      "read_only": bool((d.get("annotations") or {}).get("readOnlyHint"))})
    snap = {"source": f"fabric_backend.server tools/list, fabric-backend {package_version()}" + (f", {note}" if note else ""),
            "generated": date.today().isoformat(), "tools": tools}
    Path(out).write_text(json.dumps(snap, indent=1) + "\n", encoding="utf-8")
    return snap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.estimate")
    ap.add_argument("--snapshot", action="store_true", help="regenerate bench/krusgraph_tools.json from the installed fabric-backend")
    ap.add_argument("--note", default="", help="where the snapshot came from, e.g. 'main at 02cc8b7'")
    a = ap.parse_args(argv)
    if a.snapshot:
        snap = snapshot(note=a.note)
        print(f"{len(snap['tools'])} tools ({sum(t['read_only'] for t in snap['tools'])} read-only) -> {SNAPSHOT}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
