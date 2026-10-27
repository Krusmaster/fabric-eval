"""What a run would do and roughly cost, without calling anything: the plan behind `python -m bench.run --dry-run`.

Every number here is an ESTIMATE from the assumptions in ASSUMPTIONS, printed with them; the JSONL records of a real
run hold the measured numbers. Nothing is sent anywhere: no model, no SQL endpoint, no Krusgraph.

Input tokens grow inside a run: every step resends the tool definitions, the system prompt and the whole transcript so
far. So per run, with s steps,

    input  = sum over steps k of (fixed + (k - 1) x growth)     fixed  = prefix + question, prefix = tools + system
    output = steps x output per step                             growth = output per step + tool result per step

Without --cache all of the input is billed at the input price. With --cache (agent.request_params) the prefix is read
from the cache on every step but the setup's very first, which writes it; step 1 writes the question, and each later
step reads everything up to the previous step and writes one step's growth:

    cache read  = s x prefix + (s - 1) x question + growth x (s - 1)(s - 2) / 2
    cache write = question + (s - 1) x growth                    (read + write = input)

Cache writes cost CACHE_WRITE_FACTOR x the input price (5-minute TTL), reads cost CACHE_READ.

Tokens from characters: chars / 4 for the low end, x 1.35 for the high end (the tokenizer of current models uses up to
1.35 times as many tokens for the same text). The API's tool-use preamble is not modelled. For exact figures before a
run, the Messages API's count_tokens endpoint takes the same model, system, tools and first message.

The tool lists of setups B and C come from bench/krusgraph_tools.json, a snapshot of Krusgraph's tools/list (regenerate
with `python -m bench.estimate --snapshot` where fabric-backend's server extra is installed); a real run fetches the live
list at start and filters it the same way."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "krusgraph_tools.json"
DEFAULT_MODEL = "claude-opus-5-5"

# USD per million tokens (input, output), Anthropic first-party list prices
PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),          # prompts up to 100K tokens
}
# USD per million tokens read from the prompt cache (0.025x, 0.05x, 0.1x, 0.1x of input); writes cost input x 1.25
CACHE_READ = {"claude-fable-5-1": 0.25, "claude-opus-5-5": 0.20, "claude-sonnet-5-5": 0.20, "claude-haiku-5-5": 0.01}
CACHE_WRITE_FACTOR = 1.25                      # the 5-minute TTL, the only one the agent uses
PRICE_SOURCE = ("Anthropic's model table as of 6 October 2026; check https://platform.claude.com/docs/en/about-claude/pricing "
                "before relying on it, or pass --price-in/--price-out")

# (low, high) per run step, by setup. Judgement, not measurement: replace with the medians of a first real run.
ASSUMPTIONS = {
    "steps": {"A": (3, 6), "B": (4, 8), "C": (4, 8)},                     # model calls per question
    "tool_result_chars": {"A": (300, 3000), "B": (600, 6000), "C": (600, 6000)},   # added to the transcript per step
    "output_tokens_per_step": (150, 1500),                                # text, tool calls and thinking
    "seconds_per_step": (3, 15),                                          # model latency plus the tool call
    "chars_per_token": 4.0,
    "tokenizer_high_factor": 1.35,
}


def _tokens(chars: float) -> tuple[float, float]:
    low = chars / ASSUMPTIONS["chars_per_token"]
    return low, low * ASSUMPTIONS["tokenizer_high_factor"]


def price_table(model: str, price_in: float | None = None, price_out: float | None = None) -> tuple | None:
    """(input, output, cache read) in USD per million tokens: given prices (cache reads at 0.1 x input) or the list
    price of a known model; None for an unknown model without given prices."""
    if price_in is not None and price_out is not None:
        return price_in, price_out, price_in * 0.1
    return (*PRICES[model], CACHE_READ[model]) if model in PRICES else None


def cost_usd(prices: tuple, uncached: float, output: float, cache_read: float = 0.0, cache_write: float = 0.0) -> float:
    """USD for these token counts at `prices` (price_table's tuple)."""
    p_in, p_out, p_read = prices
    return (uncached * p_in + cache_write * p_in * CACHE_WRITE_FACTOR + cache_read * p_read + output * p_out) / 1e6


def load_snapshot(path: Path = SNAPSHOT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tool_lists(snapshot: dict | None = None) -> dict:
    """The tool definitions each setup's model would see (the `answer` tool included), B's excluded tools and the
    read-only tools C leaves out."""
    from .agent import ANSWER_TOOL
    from .mcp_tools import QA_TOOLS, offered, split_tools
    from .tools import TOOLS
    snap = snapshot if snapshot is not None else load_snapshot()
    listed = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {}),
               "annotations": {"readOnlyHint": bool(t.get("read_only"))}} for t in snap["tools"]]
    kept, excluded, shadowed = split_tools(listed, reserved=reserved_names())
    c_kept, c_left_out, c_missing = offered(kept, QA_TOOLS)
    return {"A": list(TOOLS) + [ANSWER_TOOL], "B": list(TOOLS) + kept + [ANSWER_TOOL], "C": list(TOOLS) + c_kept + [ANSWER_TOOL],
            "B_excluded": excluded, "B_shadowed": shadowed, "C_left_out": c_left_out, "C_missing": c_missing,
            "snapshot_source": snap.get("source", "?"), "snapshot_date": snap.get("generated", "?")}


def reserved_names() -> list[str]:
    """The tool names setup A already has; Krusgraph tools of the same name are left out of B."""
    from .agent import ANSWER_TOOL
    from .tools import TOOLS
    return [t["name"] for t in TOOLS] + [ANSWER_TOOL["name"]]


def per_run(setup: str, tools: list, system: str, question: str, cache: bool = False) -> dict:
    """(low, high) tokens and seconds for one run of one question. `input` is every prompt token the steps send; it
    splits into `uncached`, `cache_read` and `cache_write` (all uncached without `cache`; the module docstring has the
    formulas). The setup's one write of the prefix is added in estimate()."""
    s_lo, s_hi = ASSUMPTIONS["steps"][setup]
    r_lo, r_hi = ASSUMPTIONS["tool_result_chars"][setup]
    o_lo, o_hi = ASSUMPTIONS["output_tokens_per_step"]
    prefix, q = _tokens(len(json.dumps(tools)) + len(system)), _tokens(len(question))
    growth = (_tokens(r_lo)[0] + o_lo, _tokens(r_hi)[1] + o_hi)
    out = {k: [0.0, 0.0] for k in ("input", "uncached", "cache_read", "cache_write")}
    for i, s in enumerate((s_lo, s_hi)):
        f, qq, g = prefix[i], q[i], growth[i]
        out["input"][i] = s * (f + qq) + g * s * (s - 1) / 2
        if cache:
            out["cache_read"][i] = s * f + (s - 1) * qq + g * (s - 1) * (s - 2) / 2
            out["cache_write"][i] = qq + (s - 1) * g
        else:
            out["uncached"][i] = out["input"][i]
    sec_lo, sec_hi = ASSUMPTIONS["seconds_per_step"]
    return {**{k: tuple(v) for k, v in out.items()}, "output": (s_lo * o_lo, s_hi * o_hi),
            "seconds": (s_lo * sec_lo, s_hi * sec_hi), "tool_tokens": _tokens(len(json.dumps(tools))), "prefix_tokens": prefix}


def estimate(setups: list[str], questions: list, repeats: int, model: str, price_in: float | None = None,
             price_out: float | None = None, snapshot: dict | None = None, cache: bool = False) -> dict:
    from .agent import SYSTEM
    lists = tool_lists(snapshot)
    p = price_table(model, price_in, price_out)
    given = price_in is not None and price_out is not None
    out = {"model": model, "repeats": repeats, "questions": len(questions), "setups": {}, "cache": cache,
           "prices": p[:2] if p else None, "cache_read_price": p[2] if p else None,
           "price_source": "given on the command line (cache reads at 0.1 x input)" if given else
                           (PRICE_SOURCE if p else f"no list price known for {model!r}: pass --price-in and --price-out")}
    keys = ("input", "uncached", "cache_read", "cache_write", "output", "seconds")
    totals = {"runs": 0, **{k: [0.0, 0.0] for k in keys}}
    for s in setups:
        agg = {"runs": len(questions) * repeats, **{k: [0.0, 0.0] for k in keys}, "tools": [t["name"] for t in lists[s]],
               "tool_tokens": None}
        for q in questions:
            r = per_run(s, lists[s], SYSTEM, q.question, cache)
            agg["tool_tokens"] = r["tool_tokens"]
            for k in keys:
                for i in (0, 1):
                    agg[k][i] += r[k][i] * repeats
        if cache and questions:                                 # the setup's first step writes the prefix the others read
            for i in (0, 1):
                agg["cache_read"][i] -= r["prefix_tokens"][i]
                agg["cache_write"][i] += r["prefix_tokens"][i]
        if p:
            agg["cost_usd"] = [cost_usd(p, agg["uncached"][i], agg["output"][i], agg["cache_read"][i], agg["cache_write"][i])
                               for i in (0, 1)]
        out["setups"][s] = agg
        totals["runs"] += agg["runs"]
        for k in keys:
            for i in (0, 1):
                totals[k][i] += agg[k][i]
    if p:
        totals["cost_usd"] = [sum(out["setups"][s]["cost_usd"][i] for s in setups) for i in (0, 1)]
    out["total"] = totals
    out["excluded_b"] = lists["B_excluded"]
    out["shadowed_b"] = lists["B_shadowed"]
    out["left_out_c"] = lists["C_left_out"]
    out["missing_c"] = lists["C_missing"]
    out["snapshot"] = f"{lists['snapshot_source']} ({lists['snapshot_date']})"
    return out


def _m(x: float) -> str:
    return f"{x / 1e6:.2f} M" if x >= 1e5 else f"{x / 1e3:.0f} k"


def _t(sec: float) -> str:
    return f"{sec / 3600:.1f} h" if sec >= 5400 else f"{sec / 60:.0f} min"


def plan_text(setups: list[str], questions: list, repeats: int, model: str, env: dict[str, bool], out_dir: Path,
              run_ids: dict[str, str], effort: str | None, max_tokens: int, url: str, est: dict, seed_check: bool = True) -> str:
    """The dry-run report: exactly what would run, in which order, with which tools, and the estimate."""
    lines = ["DRY RUN: nothing is called (no model, no SQL endpoint, no Krusgraph) and nothing is written.", "",
             f"Model: {model}   effort: {effort or 'model default'}   max_tokens per response: {max_tokens}   temperature: not sent",
             f"Setups, one after the other: {', '.join(setups)}   repeats: {repeats}   questions: {len(questions)}   "
             f"runs: {len(questions) * repeats * len(setups)}",
             ("First, before any model call: the seed check, one SELECT per shop table, comparing the lakehouse with "
              "fabric_backend.seed; a difference stops the run." if seed_check else "Seed check: skipped (--skip-seed-check)."),
             ("Prompt caching: on (--cache): the tool list and system prompt, and the conversation so far, are read from the "
              "cache on later steps (5-minute TTL)." if est.get("cache") else "Prompt caching: off (--cache turns it on)."),
             "Order inside a setup: repeat 1 over every question in file order, then repeat 2, and so on.", ""]
    for s in setups:
        e = est["setups"][s]
        lines.append(f"Setup {s} -> {out_dir / (run_ids[s] + '.jsonl')}")
        if s == "A":
            lines.append("  tools: list_tables, run_sql (one T-SQL SELECT per call; anything else is refused before it reaches the endpoint), answer")
        else:
            kg = [n for n in e["tools"] if n not in ("list_tables", "run_sql", "answer")]
            which = "read-only Krusgraph tools" if s == "B" else "of Krusgraph's read-only tools, setup C's list (mcp_tools.QA_TOOLS),"
            lines.append(f"  tools: list_tables, run_sql, answer + {len(kg)} {which} from {url}:")
            lines.append("    " + ", ".join(kg))
            lines.append(f"  left out (they create items in Fabric): {', '.join(est['excluded_b']) or 'none'}")
            if est.get("shadowed_b"):
                lines.append(f"  left out (setup A's tool of the same name is kept): {', '.join(est['shadowed_b'])}")
            if s == "C":
                lines.append(f"  left out (read-only, not in setup C's list): {', '.join(est['left_out_c']) or 'none'}")
                if est.get("missing_c"):
                    lines.append(f"  in setup C's list but not in the snapshot: {', '.join(est['missing_c'])}")
            lines.append(f"  tool list from the snapshot {est['snapshot']}; the live list is fetched at start and filtered the same way")
        lines.append(f"  tool definitions: ~{e['tool_tokens'][0]:.0f}–{e['tool_tokens'][1]:.0f} tokens, "
                     + ("read from the cache on every step but the setup's first" if est.get("cache") else "resent on every step"))
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
        cached = (f" ({_m(e['cache_read'][0])}–{_m(e['cache_read'][1])} of them read from the cache, "
                  f"{_m(e['cache_write'][0])}–{_m(e['cache_write'][1])} written to it)" if est.get("cache") else "")
        lines.append(f"  {s}: {e['runs']} runs, input {_m(e['input'][0])}–{_m(e['input'][1])} tokens{cached}, output "
                     f"{_m(e['output'][0])}–{_m(e['output'][1])} tokens{cost}, {_t(e['seconds'][0])}–{_t(e['seconds'][1])}")
    t = est["total"]
    cost = f"${t['cost_usd'][0]:.2f}–${t['cost_usd'][1]:.2f}" if "cost_usd" in t else "not estimated"
    lines.append(f"  total: {t['runs']} runs, model cost {cost}, wall time {_t(t['seconds'][0])}–{_t(t['seconds'][1])} "
                 "(sequential; the capacity must run that long)")
    if est["prices"]:
        caching = (f"; cache reads ${est['cache_read_price']:.2f}, cache writes {CACHE_WRITE_FACTOR} x input (5-minute TTL)"
                   if est.get("cache") else "")
        lines.append(f"  prices: ${est['prices'][0]:.2f} input / ${est['prices'][1]:.2f} output per million tokens{caching}; "
                     f"{est['price_source']}")
    else:
        lines.append(f"  prices: {est['price_source']}")
    a = ASSUMPTIONS
    lines.append(f"  assumptions per run: steps A {a['steps']['A'][0]}–{a['steps']['A'][1]}, B and C {a['steps']['B'][0]}–{a['steps']['B'][1]}; "
                 f"tool results per step A {a['tool_result_chars']['A'][0]}–{a['tool_result_chars']['A'][1]} chars, "
                 f"B and C {a['tool_result_chars']['B'][0]}–{a['tool_result_chars']['B'][1]}; output {a['output_tokens_per_step'][0]}–"
                 f"{a['output_tokens_per_step'][1]} tokens per step; {a['seconds_per_step'][0]}–{a['seconds_per_step'][1]} s per step; "
                 + ("prompt caching on (a prefix under the model's minimum, 512 tokens on current models, is not cached on its "
                    "own; A's tool list and system prompt are near it, which this ignores)" if est.get("cache") else "no prompt caching")
                 + "; Fabric capacity usage not estimated")
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
