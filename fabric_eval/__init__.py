"""fabric-eval: verdicts, not scores — judge AI answers over a data platform by source and value."""
from .core import Question, Answer, Judgement, Run, judge, evaluate, scorecard, compare, matches, VERDICTS, NOT_IN_DATA
from .io import load_questions, load_answers, resolve_truth

__all__ = ["Question", "Answer", "Judgement", "Run", "judge", "evaluate", "scorecard", "compare", "matches",
           "VERDICTS", "NOT_IN_DATA", "load_questions", "load_answers", "resolve_truth"]
__version__ = "0.1.0"
