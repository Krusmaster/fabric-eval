"""The agent loop: Anthropic Messages API with tool use, the same for both setups except the tool list.

The model answers through the `answer` tool (value, source, refused), so the judge gets structure instead of prose.
`client` is anything with `.messages.create(...)` returning a Message — the real `anthropic.Anthropic()` or the
replaying FakeModel in the tests. Usage is read from the API's `usage` object: input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

SYSTEM = (
    "You answer questions about data in a Microsoft Fabric lakehouse: lakehouse demo_lh in workspace demo, schema shop. "
    "Use the tools to look at the tables and to compute the answer; do not guess numbers. Everything is read-only. "
    "When you have the answer, call the `answer` tool exactly once: `value` is the answer (a number, a string, or a JSON "
    "array of strings for a list), `source` is the table that holds the answered value, `refused` is true only when the "
    "data cannot answer the question. Keep working until you can call `answer`."
)

ANSWER_TOOL = {
    "name": "answer",
    "description": "Give the final answer. Call it exactly once, when you are done.",
    "input_schema": {"type": "object", "properties": {
        "value": {"description": "the answer: a number, a string, or an array of strings"},
        "source": {"type": "string", "description": "the table that holds the answered value"},
        "refused": {"type": "boolean", "description": "true only when the data cannot answer the question"},
        "note": {"type": "string", "description": "optional: how the answer was computed, one sentence"}},
        "required": ["value", "source", "refused"]},
}


def _blocks(content) -> list[dict]:
    out = []
    for b in content:
        if isinstance(b, dict):
            out.append(b)
        elif hasattr(b, "model_dump"):
            out.append(b.model_dump(exclude_none=True))
        else:
            out.append({k: getattr(b, k) for k in ("type", "text", "id", "name", "input") if hasattr(b, k)})
    return out


def _usage(message) -> dict:
    u = getattr(message, "usage", None)
    get = (lambda k: (u.get(k) if isinstance(u, dict) else getattr(u, k, None)) or 0) if u is not None else (lambda k: 0)
    return {"input_tokens": int(get("input_tokens")), "output_tokens": int(get("output_tokens")),
            "cache_read_tokens": int(get("cache_read_input_tokens")), "cache_creation_tokens": int(get("cache_creation_input_tokens"))}


def run_question(client, model: str, question, toolsets: list, run_id: str, setup: str, repeat: int = 1,
                 temperature: float = 0.0, max_steps: int = 12, max_tokens: int = 1024, system: str = SYSTEM) -> dict:
    """One question, one run: returns the record that goes to the JSONL (without the verdict)."""
    tools = [t for ts in toolsets for t in ts.tools] + [ANSWER_TOOL]
    messages = [{"role": "user", "content": question.question}]
    rec = {"run_id": run_id, "setup": setup, "question_id": question.id, "repeat": repeat, "model": model,
           "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "steps": 0,
           "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
           "tool_calls": {}, "sql_statements": 0, "sql_failed": 0, "graphql_calls": 0, "graphql_failed": 0,
           "mcp_calls": 0, "mcp_failed": 0, "answer": None, "error": None}
    t0 = time.monotonic()
    sql_before = [(getattr(ts, "statements", 0), getattr(ts, "failed", 0)) for ts in toolsets]
    try:
        for _ in range(max_steps):
            rec["steps"] += 1
            message = client.messages.create(model=model, system=system, tools=tools, messages=messages,
                                             max_tokens=max_tokens, temperature=temperature)
            for k, v in _usage(message).items():
                rec[k] += v
            blocks = _blocks(message.content)
            messages.append({"role": "assistant", "content": blocks})
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                rec["answer"] = {"value": text, "source": None, "refused": False, "text": text, "structured": False}
                break
            results = []
            done = False
            for u in uses:
                name, args = u["name"], u.get("input") or {}
                rec["tool_calls"][name] = rec["tool_calls"].get(name, 0) + 1
                if name == "answer":
                    rec["answer"] = {"value": args.get("value"), "source": args.get("source"), "refused": bool(args.get("refused")),
                                     "text": str(args.get("note") or ""), "structured": True}
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": "recorded"})
                    done = True
                    continue
                ts = next((x for x in toolsets if x.handles(name)), None)
                if ts is None:
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": f"unknown tool {name}", "is_error": True})
                    continue
                text, is_error = ts.call(name, args, question.id, repeat)
                if name == "query_graphql":
                    rec["graphql_calls"] += 1
                    rec["graphql_failed"] += int(is_error)
                if not hasattr(ts, "statements"):                    # an MCP toolset
                    rec["mcp_calls"] += 1
                    rec["mcp_failed"] += int(is_error)
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            if done:
                break
        else:
            rec["error"] = f"no answer within {max_steps} steps"
    except Exception as e:  # noqa: BLE001 — recorded, never hidden
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["seconds"] = round(time.monotonic() - t0, 3)
    for ts, (s0, f0) in zip(toolsets, sql_before):
        if hasattr(ts, "statements"):
            rec["sql_statements"] += ts.statements - s0
            rec["sql_failed"] += ts.failed - f0
    rec["tokens_total"] = rec["input_tokens"] + rec["output_tokens"] + rec["cache_read_tokens"] + rec["cache_creation_tokens"]
    rec["transcript_chars"] = len(json.dumps(messages, default=str))
    return rec


def verdict_for(question, rec: dict) -> dict:
    """fabric-eval's judge, reduced to the four verdicts the rig reports: correct, wrong, refused, error."""
    from fabric_eval import Answer, judge
    a = rec.get("answer") or {}
    if rec.get("error"):
        ans = Answer(question.id, error=rec["error"])
    else:
        ans = Answer(question.id, refused=bool(a.get("refused")), value=a.get("value"), source=a.get("source"), text=a.get("text", ""))
    j = judge(question, ans)
    v = "error" if j.verdict == "error" else "refused" if ans.refused else "correct" if j.verdict == "correct" else "wrong"
    return {"verdict": v, "judge_verdict": j.verdict, "judge_detail": j.detail, "expected": question.answer}
