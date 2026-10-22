"""fabric-eval CLI.

  fabric-eval run questions.yaml --answers copilot=answers_copilot.jsonl --answers gpt=answers_gpt.jsonl \\
      [--out scorecard.md] [--save runs/]
  fabric-eval compare runs/copilot_v1.json runs/copilot_v2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Run, evaluate, scorecard, compare
from .io import load_questions, load_answers


def cmd_run(a) -> int:
    qs = load_questions(a.questions)
    runs = []
    for spec in a.answers:
        model, _, path = spec.partition("=")
        if not path:
            model, path = Path(spec).stem, spec
        runs.append(evaluate(qs, load_answers(path), model))
    md = scorecard(runs, qs)
    print(md)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    if a.save:
        for r in runs:
            r.save(Path(a.save) / f"{r.model}.json")
    return 0


def cmd_compare(a) -> int:
    rows = compare(Run.load(a.previous), Run.load(a.current))
    if not rows:
        print("no verdict changed")
        return 0
    print("| question | before | after | direction | detail |\n| --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['question_id']} | {r['before']} | {r['after']} | {r['direction']} | {r['detail']} |")
    return 1 if any(r["direction"] == "worse" for r in rows) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fabric-eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run"); s.add_argument("questions"); s.add_argument("--answers", action="append", required=True)
    s.add_argument("--out"); s.add_argument("--save"); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("compare"); s.add_argument("previous"); s.add_argument("current"); s.set_defaults(fn=cmd_compare)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
