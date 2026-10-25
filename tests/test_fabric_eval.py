from pathlib import Path

from fabric_eval import (Question, Answer, judge, evaluate, scorecard, compare, matches, load_questions,
                         load_answers, resolve_truth, Run, NOT_IN_DATA)
from fabric_eval.cli import main

EX = Path(__file__).resolve().parent.parent / "examples"


def test_matches_numbers_strings_sets_rows():
    assert matches(10, "10") and matches(2.1, 2.08, tolerance=0.05) and not matches(2.1, 2.0, tolerance=0.05)
    assert matches(1234, "1,234") and matches(1234, "1'234") and matches(48.5, "48,5")
    assert matches(1000, 1050, tolerance=0.1, rel=True)
    assert matches("Harbour Bikes", "  harbour bikes ") and not matches("Hamburg", "Leipzig")
    assert matches(["Bikes", "Tea"], ["tea", "Bikes"]) and not matches(["Bikes", "Tea"], ["Bikes"])
    assert matches([{"id": 1}, {"id": 2}], [{"id": 2, "x": 0}, {"id": 1}])


def test_four_verdicts():
    q = Question("q", "?", "orders", answer=23)
    assert judge(q, Answer("q", value=23, source="orders")).verdict == "correct"
    assert judge(q, Answer("q", value=22, source="orders")).verdict == "wrong"
    j = judge(q, Answer("q", value=23, source="customers"))
    assert j.verdict == "wrong_source" and "luck" in j.detail            # right number, wrong place
    assert judge(q, Answer("q", refused=True)).verdict == "wrong"        # refused but the answer exists
    assert judge(q, Answer("q", value=23)).verdict == "correct"          # no source reported: value decides
    n = Question("n", "?", NOT_IN_DATA)
    assert judge(n, Answer("n", refused=True)).verdict == "correctly_refused"
    assert judge(n, Answer("n", value=48200, source="orders")).verdict == "wrong_source"
    assert judge(n, Answer("n", value=48200)).verdict == "wrong"
    assert judge(q, Answer("q", error="timeout")).verdict == "error"


def test_example_scorecard_and_compare(tmp_path):
    qs = load_questions(EX / "questions.yaml")
    assert len(qs) == 15 and sum(1 for q in qs if not q.in_data) == 3
    a = evaluate(qs, load_answers(EX / "answers_model_a.jsonl"), "model_a")
    b = evaluate(qs, load_answers(EX / "answers_model_b.jsonl"), "model_b")
    assert a.score == "14/15" and a.counts()["correctly_refused"] == 3
    assert b.score == "11/15" and b.counts()["wrong_source"] == 3
    md = scorecard([a, b], qs)
    assert "| model_b | **11/15** | 10 | 1 | 3 | 1 | 0 |" in md and "q08" in md
    rows = compare(a, b)
    ids = {r["question_id"]: r for r in rows}
    assert ids["q03"]["after"] == "wrong_source" and ids["q03"]["direction"] == "worse"
    assert ids["q12"]["direction"] == "better"
    p = a.save(tmp_path / "a.json")
    assert Run.load(p).score == "14/15"


def test_resolve_truth_from_sql_and_graphql():
    qs = [Question("a", "?", "customers", truth={"sql": "SELECT COUNT(*) FROM c"}),
          Question("b", "?", "categories", truth={"sql": "SELECT name FROM cat"}),
          Question("c", "?", "orders", truth={"graphql": "{ orders { items { order_id } } }", "path": "orders.items"})]

    def execute(sql):
        return [{"n": 10}] if "COUNT" in sql else [{"name": "Bikes"}, {"name": "Tea"}]

    def graphql(q):
        return {"orders": {"items": [{"order_id": 100}, {"order_id": 101}]}}

    resolve_truth(qs, execute=execute, graphql=graphql)
    assert qs[0].answer == 10 and qs[1].answer == ["Bikes", "Tea"] and qs[2].answer == [100, 101]


def test_cli(tmp_path):
    assert main(["run", str(EX / "questions.yaml"), "--answers", f"a={EX / 'answers_model_a.jsonl'}",
                 "--answers", f"b={EX / 'answers_model_b.jsonl'}", "--out", str(tmp_path / "s.md"), "--save", str(tmp_path)]) == 0
    assert (tmp_path / "s.md").exists() and (tmp_path / "a.json").exists()
    assert main(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 1   # something got worse
