"""Load questions (YAML/JSON), answers (JSONL), and compute truth from a query when asked."""
from __future__ import annotations

import json
from pathlib import Path

from .core import Question, Answer, NOT_IN_DATA


def load_questions(path) -> list[Question]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    items = data["questions"] if isinstance(data, dict) else data
    out = []
    for d in items:
        d = dict(d)
        d.setdefault("source", NOT_IN_DATA)
        out.append(Question(**{k: d[k] for k in d if k in Question.__dataclass_fields__}))
    return out


def load_answers(path) -> dict[str, Answer]:
    """JSONL, one object per line: {"question_id": "q1", "value": 42, "source": "orders", "refused": false, "text": "..."}"""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        a = Answer(**{k: d[k] for k in d if k in Answer.__dataclass_fields__})
        out[a.question_id] = a
    return out


def resolve_truth(questions: list[Question], execute=None, graphql=None) -> list[Question]:
    """Fill q.answer from q.truth: {"sql": "..."} via execute(sql) -> rows, or {"graphql": "...", "path": "a.b"} via
    graphql(query) -> dict. A single-cell result becomes a scalar; one column becomes a list; else rows."""
    for q in questions:
        if not q.truth:
            continue
        if "sql" in q.truth:
            if execute is None:
                raise ValueError(f"{q.id}: truth.sql needs execute=")
            rows = [dict(r) if hasattr(r, "keys") else r for r in execute(q.truth["sql"])]
            q.answer = _shape(rows)
        elif "graphql" in q.truth:
            if graphql is None:
                raise ValueError(f"{q.id}: truth.graphql needs graphql=")
            data = graphql(q.truth["graphql"])
            for part in (q.truth.get("path") or "").split("."):
                if part:
                    data = data[part]
            q.answer = _shape(data if isinstance(data, list) else [data])
    return questions


def _shape(rows):
    if not rows:
        return []
    first = rows[0]
    if isinstance(first, dict):
        keys = list(first.keys())
        if len(rows) == 1 and len(keys) == 1:
            return first[keys[0]]
        if len(keys) == 1:
            return [r[keys[0]] for r in rows]
        return rows
    if isinstance(first, (list, tuple)):
        if len(rows) == 1 and len(first) == 1:
            return first[0]
        if len(first) == 1:
            return [r[0] for r in rows]
        return [list(r) for r in rows]
    return rows if len(rows) > 1 else first
