"""Build bench/REPORT.md from bench/results/*.jsonl.

Per setup: share of correct answers; median and spread (p25–p75, min–max) over the CORRECT answers for tokens, tool
calls, failed queries, Fabric CPU time and wall time. B against A: the difference of per-question means with a
bootstrap confidence interval over the questions (resample question ids with replacement, 2000 times, seed 0). The
energy/CO2 section uses bench/factors.yaml and prints "not estimated" for every factor without a source."""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
METRICS = [("tokens_total", "tokens (all)"), ("input_tokens", "input tokens"), ("output_tokens", "output tokens"),
           ("cache_read_tokens", "tokens read from cache"), ("tool_calls_total", "tool calls"), ("failed_queries", "failed queries"),
           ("sql_statements", "SQL statements"), ("graphql_calls", "GraphQL calls"), ("fabric_cpu_ms", "Fabric CPU ms"),
           ("fabric_scanned_mb", "Fabric data scanned MB"), ("seconds", "wall time s")]


def load_results(paths: list[Path]) -> list[dict]:
    recs = []
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["tool_calls_total"] = sum((r.get("tool_calls") or {}).values())
                r["failed_queries"] = int(r.get("sql_failed", 0)) + int(r.get("graphql_failed", 0))
                fab = r.get("fabric") or {}
                r["fabric_cpu_ms"] = fab.get("cpu_ms") if fab.get("statements") else None
                r["fabric_scanned_mb"] = (fab.get("data_scanned_remote_mb", 0) + fab.get("data_scanned_memory_mb", 0) + fab.get("data_scanned_disk_mb", 0)) if fab.get("statements") else None
                recs.append(r)
    return recs


def spread(values: list[float]) -> dict | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    q = statistics.quantiles(vals, n=4) if len(vals) >= 2 else [vals[0], vals[0], vals[0]]
    return {"n": len(vals), "median": statistics.median(vals), "p25": q[0], "p75": q[2], "min": min(vals), "max": max(vals)}


def per_setup(recs: list[dict]) -> dict:
    out = {}
    for setup in sorted({r["setup"] for r in recs}):
        rs = [r for r in recs if r["setup"] == setup]
        judged = [r for r in rs if r["verdict"] != "error"]
        correct = [r for r in rs if r["verdict"] == "correct"]
        counts = {v: sum(1 for r in rs if r["verdict"] == v) for v in ("correct", "wrong", "refused", "error")}
        out[setup] = {"runs": len(rs), "questions": len({r["question_id"] for r in rs}), "counts": counts,
                      "accuracy": (len(correct) / len(judged)) if judged else None,
                      "per_correct": {m: spread([r.get(m) for r in correct]) for m, _ in METRICS},
                      "models": sorted({r.get("model", "") for r in rs}), "run_ids": sorted({r.get("run_id", "") for r in rs})}
    return out


def per_question_means(recs: list[dict], setup: str, metric: str, correct_only: bool) -> dict[str, float]:
    by = defaultdict(list)
    for r in recs:
        if r["setup"] != setup:
            continue
        if correct_only and r["verdict"] != "correct":
            continue
        v = 1.0 if metric == "accuracy" and r["verdict"] == "correct" else 0.0 if metric == "accuracy" else r.get(metric)
        if metric == "accuracy" and r["verdict"] == "error":
            continue
        if v is not None:
            by[r["question_id"]].append(float(v))
    return {q: statistics.fmean(v) for q, v in by.items()}


def bootstrap_difference(a: dict[str, float], b: dict[str, float], samples: int = 2000, seed: int = 0) -> dict | None:
    """B - A over the questions both setups have; percentile interval of the mean difference."""
    common = sorted(set(a) & set(b))
    if not common:
        return None
    diffs = [b[q] - a[q] for q in common]
    rnd = random.Random(seed)
    means = []
    for _ in range(samples):
        pick = [diffs[rnd.randrange(len(diffs))] for _ in diffs]
        means.append(statistics.fmean(pick))
    means.sort()
    lo, hi = means[int(0.025 * samples)], means[min(samples - 1, int(0.975 * samples))]
    return {"questions": len(common), "mean_a": statistics.fmean(a[q] for q in common), "mean_b": statistics.fmean(b[q] for q in common),
            "difference": statistics.fmean(diffs), "ci95": [lo, hi], "samples": samples}


def differences(recs: list[dict]) -> dict:
    out = {"accuracy": bootstrap_difference(per_question_means(recs, "A", "accuracy", False), per_question_means(recs, "B", "accuracy", False))}
    for m, _ in METRICS:
        out[m] = bootstrap_difference(per_question_means(recs, "A", m, True), per_question_means(recs, "B", m, True))
    return out


# ------------------------------------------------------------------ energy and CO2 -------------

def _factor(f: dict | None) -> tuple[float, float] | None:
    if not f or not f.get("source") or f.get("low") is None or f.get("high") is None:
        return None
    return float(f["low"]), float(f["high"])


def energy_co2(setups: dict, factors: dict, region: str) -> dict:
    """Intervals per setup and per correct answer (median), from sourced factors only."""
    out = {}
    tok = _factor((factors.get("tokens") or {}).get("energy_wh_per_1000_tokens"))
    cpu = _factor((factors.get("fabric") or {}).get("cpu_hour_kwh"))
    grid = _factor((factors.get("grid") or {}).get(region))
    for setup, s in setups.items():
        e: dict = {}
        tokens = (s["per_correct"].get("tokens_total") or {}).get("median")
        cpu_ms = (s["per_correct"].get("fabric_cpu_ms") or {}).get("median")
        model_wh = (tokens / 1000 * tok[0], tokens / 1000 * tok[1]) if tok and tokens is not None else None
        fabric_wh = (cpu_ms / 3.6e6 * cpu[0] * 1000, cpu_ms / 3.6e6 * cpu[1] * 1000) if cpu and cpu_ms is not None else None
        e["model_wh"] = model_wh
        e["fabric_wh"] = fabric_wh
        e["model_co2_g"] = (model_wh[0] / 1000 * grid[0], model_wh[1] / 1000 * grid[1]) if model_wh and grid else None
        e["fabric_co2_g"] = (fabric_wh[0] / 1000 * grid[0], fabric_wh[1] / 1000 * grid[1]) if fabric_wh and grid else None
        out[setup] = e
    return out


# ------------------------------------------------------------------ markdown -------------------

def _fmt(x, nd=1) -> str:
    if x is None:
        return "–"
    return f"{x:.{nd}f}" if isinstance(x, float) and not x.is_integer() else f"{int(x)}" if isinstance(x, float) else str(x)


def _interval(iv) -> str:
    return "not estimated" if iv is None else f"{iv[0]:.3g} – {iv[1]:.3g}"


def markdown(recs: list[dict], factors: dict, region: str) -> str:
    setups = per_setup(recs)
    diffs = differences(recs)
    out = ["# Agent efficiency: with and without Krusgraph", "",
           "Measured, not claimed. Setup A has `list_tables` and `run_sql` (read-only T-SQL); setup B has the same two tools plus "
           "Krusgraph's MCP tools. Same model, system prompt (apart from the tool list), temperature and questions; the raw "
           "records are the JSONL files next to this report. Resource figures are over the **correct** answers only.", ""]
    if not recs:
        return "\n".join(out + ["No results yet."]) + "\n"
    out += ["## Runs", "", "| setup | runs | questions | correct | wrong | refused | error | share correct | model(s) | run id(s) |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, s in setups.items():
        c = s["counts"]
        out.append(f"| {k} | {s['runs']} | {s['questions']} | {c['correct']} | {c['wrong']} | {c['refused']} | {c['error']} | "
                   f"{'–' if s['accuracy'] is None else f'{100 * s['accuracy']:.0f} %'} | {', '.join(s['models'])} | {', '.join(s['run_ids'])} |")
    out += ["", "## Per correct answer (median, p25–p75, min–max)", ""]
    out += ["| metric | " + " | ".join(setups) + " |", "| --- | " + " | ".join("---" for _ in setups) + " |"]
    for m, label in METRICS:
        cells = []
        for s in setups.values():
            sp = s["per_correct"].get(m)
            cells.append("–" if not sp else f"{_fmt(sp['median'])} ({_fmt(sp['p25'])}–{_fmt(sp['p75'])}, {_fmt(sp['min'])}–{_fmt(sp['max'])}, n={sp['n']})")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out += ["", "## B against A", "",
            "Difference of per-question means (B − A) with a 95 % bootstrap interval over the questions (2000 resamples). "
            "Accuracy uses every judged run; the other rows use correct runs only, so a question with no correct run in one "
            "setup drops out of that row (the `questions` column says how many remain). An interval that contains 0 means the "
            "data does not separate the two setups on that metric.", "",
            "| metric | questions | mean A | mean B | B − A | 95 % interval |", "| --- | --- | --- | --- | --- | --- |"]
    for m, label in [("accuracy", "share correct")] + METRICS:
        d = diffs.get(m)
        if d is None:
            out.append(f"| {label} | 0 | – | – | – | not comparable |")
        else:
            out.append(f"| {label} | {d['questions']} | {d['mean_a']:.3g} | {d['mean_b']:.3g} | {d['difference']:+.3g} | {d['ci95'][0]:+.3g} to {d['ci95'][1]:+.3g} |")
    n_runs = {k: s["runs"] for k, s in setups.items()}
    out += ["", f"Runs per setup: {', '.join(f'{k}: {v}' for k, v in n_runs.items())}. Questions with Fabric CPU figures: "
            + ", ".join(f"{k}: {(s['per_correct'].get('fabric_cpu_ms') or {}).get('n', 0)}" for k, s in setups.items())
            + " (queryinsights data is attached by bench/fabric_metrics.py; in setup B Krusgraph's own statements are untagged and are reported separately by that script).", ""]
    ec = energy_co2(setups, factors, region)
    out += ["## Energy and CO₂ (optional, factor-based)", "",
            f"Region for the grid factor: `{region}`. Only factors with a source in bench/factors.yaml are used; everything else is "
            "\"not estimated\". Figures are per correct answer (median) and always an interval.", "",
            "| setup | model energy Wh | Fabric CPU energy Wh | model CO₂ g | Fabric CO₂ g |", "| --- | --- | --- | --- | --- |"]
    for k, e in ec.items():
        out.append(f"| {k} | {_interval(e['model_wh'])} | {_interval(e['fabric_wh'])} | {_interval(e['model_co2_g'])} | {_interval(e['fabric_co2_g'])} |")
    srcs = []
    for group, items in factors.items():
        for name, f in (items or {}).items():
            if isinstance(f, dict):
                srcs.append(f"- `{group}.{name}`: " + (f"{f.get('low')}–{f.get('high')}, {f.get('source')} ({f.get('date')})" if _factor(f) else "no source yet"))
    out += ["", "Factors:"] + srcs
    out += ["", "## Limits", "",
            "- The shop schema is a synthetic demo with seven small tables, not a customer's lakehouse; the questions were written by the "
            "people who built Krusgraph.",
            "- Rebound: a tool that makes answers cheaper can lead to more questions being asked; this measures cost per answered question, "
            "not total use.",
            "- Model version and date: the model id is in every record; a later model, prompt or Krusgraph version changes the numbers.",
            "- Caching: prompt-cache reads are counted separately; a warm cache on repeat 2 and 3 lowers input tokens for both setups, "
            "and B's longer tool list caches differently from A's.",
            "- Krusgraph's own server (Azure Container Apps) is not in these figures unless measured separately; in setup B its reads of the "
            "SQL endpoint show up in queryinsights without a bench tag.",
            "- queryinsights attributes CPU time per statement with a delay of up to 15 minutes and omits system queries; columns the view "
            "does not have are reported as missing rather than guessed.",
            "- Three repeats per question is enough to see large differences, not small ones; the bootstrap interval says how much the "
            "twenty questions support.", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.report")
    ap.add_argument("results", nargs="*", help="JSONL files (default: bench/results/*.jsonl)")
    ap.add_argument("--out", default=str(HERE / "REPORT.md"))
    ap.add_argument("--factors", default=str(HERE / "factors.yaml"))
    ap.add_argument("--region", default="swedencentral")
    a = ap.parse_args(argv)
    import yaml
    paths = [Path(p) for p in a.results] or sorted((HERE / "results").glob("*.jsonl"))
    factors = yaml.safe_load(Path(a.factors).read_text(encoding="utf-8")) or {}
    md = markdown(load_results(paths), factors, a.region)
    Path(a.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
