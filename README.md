# fabric-eval

**Verdicts, not scores. Judge AI answers over your data by *where they came from*, not only by
whether the number matched.**

**The problem.** You put Copilot, an agent or a model in front of your lakehouse and ask it
fifteen questions. Nine come back right. Is that good? You cannot tell, because "right" hides
three different things: an answer computed from the table that holds it; a lucky match pulled
from a table that does not; and a confident number for a question the data cannot answer at all.
The third one is the dangerous one, and a single accuracy score counts it the same as the first.

**What this does.** Four verdicts per question, with the expected *source* as part of the
expectation:

| verdict | meaning |
| --- | --- |
| `correct` | answered from the expected table/model, value matches |
| `correctly_refused` | the answer is not in the data, and the model said so |
| `wrong_source` | answered from a model that does not hold the answer — *even if the value matched* |
| `wrong` | value does not match, or the model refused when the answer existed |

"Answered from the wrong model" is worse than "not in the data". The first looks like success.

```
fabric-eval run questions.yaml --answers copilot=answers_copilot.jsonl --answers agent=answers_agent.jsonl
```

```
| model   | score     | correct | correctly refused | wrong source | wrong | error |
| copilot | **14/15** | 11      | 3                 | 0            | 1     | 0     |
| agent   | **11/15** | 10      | 1                 | 3            | 1     | 0     |
```

followed by the per-question matrix, and `fabric-eval compare before.json after.json` lists only
the questions whose verdict changed — the rows you actually read after a model upgrade.

## Questions

```yaml
questions:
  - id: q02
    question: How many orders were placed in October 2026?
    source: orders                     # the table or model the answer must come from
    answer: 23                         # literal truth for the fixture...
    truth: { sql: "SELECT COUNT(*) FROM shop.orders WHERE order_date >= '2026-10-01' AND order_date < '2026-11-01'" }
  - id: q08
    question: What was the revenue in October 2026?
    source: not_in_data                # no price column anywhere: the right answer is a refusal
```

`truth.sql` (or `truth.graphql` + `path`) recomputes the answer against a live copy, so the same
question file survives a regenerated dataset. Numbers accept `tolerance` (absolute, or `rel: true`),
sets are order-insensitive, rows are compared as sets of dicts.

## Answers

The eval does not run the model. It judges recorded answers, one JSONL line per question:

```json
{"question_id": "q02", "value": 23, "source": "orders", "text": "There were 23 orders in October 2026."}
{"question_id": "q08", "refused": true, "text": "There is no price information in the data."}
```

`source` is what the model says it used, if it reports one (Copilot cites the semantic model; an
agent over GraphQL can cite the type). Without it, the value decides. How you get from a chat
window to that JSONL is your adapter — a notebook that calls the endpoint and parses the reply is
the usual one, and `examples/` has the format.

## Example

`examples/questions.yaml` is fifteen questions over a synthetic shop schema (customers, orders,
lines, products, categories, addresses, audit log), three of them unanswerable on purpose, with
two recorded answer sets: one careful model, one confident one. Run it:

```
fabric-eval run examples/questions.yaml --answers a=examples/answers_model_a.jsonl --answers b=examples/answers_model_b.jsonl
```

## Status

v0.1.0. Judging, matching, truth resolution and the CLI are tested on the example. Not yet run
against a live Copilot or agent — the first real question set is the next step, and the status
line changes when it has been.

## License

MIT
