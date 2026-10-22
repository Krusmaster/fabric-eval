"""Verdicts, not scores.

An AI answer over a data platform is judged with four verdicts — and the expected *source* is part
of the expectation, because an answer pulled from a model that does not contain it is luck, not
success, even when the number happens to match:

  correct             answered from the expected source, value matches
  correctly_refused   the answer is not in the data, and the model said so
  wrong_source        answered from a model/table that does not hold the answer
  wrong               answered, value does not match — or refused when the answer existed

A fifth, `error`, is for the harness (timeout, exception). It is never counted as wrong.

Questions are a YAML/JSON list; answers come from any provider (a callable, or a JSONL file of
recorded answers, so the eval does not care which agent, Copilot or model produced them). Truth
is either a literal `answer` or a `truth` query run against the data, so expectations survive a
regenerated synthetic dataset.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

VERDICTS = ("correct", "correctly_refused", "wrong_source", "wrong", "error")
NOT_IN_DATA = "not_in_data"


@dataclass
class Question:
    id: str
    question: str
    source: str                          # expected source (table/model name) or NOT_IN_DATA
    answer: object = None                # literal truth (number, string, list, list of dicts)
    truth: dict | None = None            # {"sql": "..."} or {"graphql": "...", "path": "a.b.items"} computed at run time
    tolerance: float = 0.0               # absolute, or relative if rel=True
    rel: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def in_data(self) -> bool:
        return self.source != NOT_IN_DATA


@dataclass
class Answer:
    question_id: str
    refused: bool = False
    value: object = None                 # what the model answered, parsed (number/string/list/rows)
    source: str | None = None            # which table/model the model says it used, if it reports one
    text: str = ""                       # raw answer text, kept for the report
    error: str | None = None
    seconds: float | None = None


@dataclass
class Judgement:
    question_id: str
    verdict: str
    detail: str = ""
    expected: object = None
    got: object = None
    expected_source: str = ""
    got_source: str | None = None


# ------------------------------------------------------------------ matching -----------------

def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(" ", "").replace(" ", "")
        s = re.sub(r"(?<=\d)[,'](?=\d{3}\b)", "", s)       # thousands separators 1,234 / 1'234
        s = s.replace(",", ".")
        m = re.fullmatch(r"[-+]?\d+(\.\d+)?", s)
        return float(s) if m else None
    return None


def _norm_str(x) -> str:
    return re.sub(r"\s+", " ", str(x)).strip().lower()


def matches(expected, got, tolerance: float = 0.0, rel: bool = False) -> bool:
    if expected is None:
        return got is None
    en, gn = _num(expected), _num(got)
    if en is not None and gn is not None:
        tol = abs(en) * tolerance if rel else tolerance
        return math.isclose(en, gn, abs_tol=tol) if tol else en == gn
    if isinstance(expected, dict) and isinstance(got, dict):
        return all(k in got and matches(v, got[k], tolerance, rel) for k, v in expected.items())
    if isinstance(expected, (list, tuple, set)):
        if not isinstance(got, (list, tuple, set)):
            return False
        if all(isinstance(e, dict) for e in expected):
            return len(expected) == len(got) and all(any(matches(e, g, tolerance, rel) for g in got) for e in expected)
        return {_norm_str(e) for e in expected} == {_norm_str(g) for g in got}
    return _norm_str(expected) == _norm_str(got)


# ------------------------------------------------------------------ judging ------------------

def judge(q: Question, a: Answer) -> Judgement:
    j = Judgement(q.id, "error", expected=q.answer, got=a.value, expected_source=q.source, got_source=a.source)
    if a.error:
        j.detail = a.error
        return j
    if not q.in_data:
        if a.refused:
            j.verdict, j.detail = "correctly_refused", "not in the data, and the model said so"
        else:
            j.verdict = "wrong_source" if a.source else "wrong"
            j.detail = f"answered from {a.source}" if a.source else "answered something the data does not hold"
        return j
    if a.refused:
        j.verdict, j.detail = "wrong", "refused, but the answer exists"
        return j
    if a.source and _norm_str(a.source) != _norm_str(q.source):
        j.verdict, j.detail = "wrong_source", f"expected {q.source}, answered from {a.source}" + \
            (" (value matched — luck, not success)" if matches(q.answer, a.value, q.tolerance, q.rel) else "")
        return j
    if matches(q.answer, a.value, q.tolerance, q.rel):
        j.verdict, j.detail = "correct", ""
    else:
        j.verdict, j.detail = "wrong", f"expected {q.answer!r}, got {a.value!r}"
    return j


# ------------------------------------------------------------------ runs ---------------------

@dataclass
class Run:
    model: str
    judgements: list[Judgement]
    taken_at: str = ""

    def counts(self) -> dict[str, int]:
        c = {v: 0 for v in VERDICTS}
        for j in self.judgements:
            c[j.verdict] += 1
        return c

    @property
    def score(self) -> str:
        c = self.counts()
        n = len(self.judgements) - c["error"]
        return f"{c['correct'] + c['correctly_refused']}/{n}"

    def to_dict(self) -> dict:
        return {"model": self.model, "taken_at": self.taken_at, "judgements": [asdict(j) for j in self.judgements]}

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        return cls(d["model"], [Judgement(**j) for j in d["judgements"]], d.get("taken_at", ""))

    def save(self, path) -> Path:
        p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=1, default=str), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path) -> "Run":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def evaluate(questions: list[Question], answers: dict[str, Answer], model: str) -> Run:
    from datetime import datetime, timezone
    js = []
    for q in questions:
        a = answers.get(q.id) or Answer(q.id, error="no answer recorded")
        js.append(judge(q, a))
    return Run(model, js, datetime.now(timezone.utc).isoformat(timespec="seconds"))


def scorecard(runs: list[Run], questions: list[Question] | None = None) -> str:
    """Markdown: one row per model with verdict counts, then the per-question matrix."""
    out = ["| model | score | correct | correctly refused | wrong source | wrong | error |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        c = r.counts()
        out.append(f"| {r.model} | **{r.score}** | {c['correct']} | {c['correctly_refused']} | {c['wrong_source']} | {c['wrong']} | {c['error']} |")
    if questions:
        short = {"correct": "✅", "correctly_refused": "🚫✅", "wrong_source": "⚠️ source", "wrong": "❌", "error": "💥"}
        out += ["", "| # | question | expected source | " + " | ".join(r.model for r in runs) + " |",
                "| --- | --- | --- | " + " | ".join("---" for _ in runs) + " |"]
        by = {r.model: {j.question_id: j for j in r.judgements} for r in runs}
        for q in questions:
            cells = [short.get(by[r.model][q.id].verdict, "?") if q.id in by[r.model] else "" for r in runs]
            out.append(f"| {q.id} | {q.question} | {q.source} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def compare(previous: Run, current: Run) -> list[dict]:
    """Questions whose verdict changed between two runs — the only rows worth reading after a model change."""
    prev = {j.question_id: j for j in previous.judgements}
    rows = []
    for j in current.judgements:
        p = prev.get(j.question_id)
        if p and p.verdict != j.verdict:
            rank = {"correct": 3, "correctly_refused": 3, "wrong": 1, "wrong_source": 0, "error": 2}
            rows.append({"question_id": j.question_id, "before": p.verdict, "after": j.verdict,
                         "direction": "better" if rank[j.verdict] > rank[p.verdict] else "worse", "detail": j.detail})
    return rows
