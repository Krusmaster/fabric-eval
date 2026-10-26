"""The two baseline tools every setup has: list_tables(schema) and run_sql(query), read-only T-SQL against the
lakehouse's SQL analytics endpoint. Only SELECT (or WITH … SELECT) is run; everything else is refused with the reason;
errors from the endpoint are returned to the model verbatim (nothing is masked). Every statement is tagged with a
comment /* bench:<run_id>:<question_id>:r<repeat> */ so queryinsights can attribute it afterwards."""
from __future__ import annotations

import json
import re
import struct
from typing import Callable

MAX_ROWS = 200
MAX_CHARS = 20_000
_STARTS = re.compile(r"^\s*(with|select)\b", re.I)
# T-SQL runs several statements in one batch without a ';' (`SELECT 1 DROP TABLE t`), so every word that starts a
# statement that writes, waits, switches database or reaches outside is refused wherever it appears, string literals
# included: a false refusal costs the model one retry, a missed one could change the lakehouse.
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|into|backup|restore|"
                        r"dbcc|bulk|openrowset|openquery|opendatasource|openxml|waitfor|shutdown|kill|reconfigure|use)\b", re.I)

TOOLS = [
    {"name": "list_tables", "description": "List the tables and their columns (name and SQL type) in one schema of the lakehouse's SQL analytics endpoint.",
     "input_schema": {"type": "object", "properties": {"schema": {"type": "string", "description": "schema name, e.g. shop"}}, "required": ["schema"]}},
    {"name": "run_sql", "description": "Run one read-only T-SQL SELECT statement against the lakehouse's SQL analytics endpoint and get the rows back (at most 200). Anything but SELECT is refused.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "a single T-SQL SELECT statement"}}, "required": ["query"]}},
]


class RefusedSQL(ValueError):
    pass


def guard(sql: str) -> str:
    """The statement as it will be run, or RefusedSQL saying why not."""
    s = (sql or "").strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise RefusedSQL("empty statement")
    if ";" in s:
        raise RefusedSQL("one statement per call; remove the ';'")
    if not _STARTS.match(s):
        raise RefusedSQL("only SELECT (or WITH … SELECT) is allowed")
    bad = _FORBIDDEN.search(s)
    if bad:
        raise RefusedSQL(f"read-only: {bad.group(0).upper()} is not allowed")
    return s


def tag(sql: str, run_id: str, question_id: str, repeat: int) -> str:
    return f"/* bench:{run_id}:{question_id}:r{repeat} */ {sql}"


def sqlserver_execute(server: str, database: str, token: str, login_timeout: int = 30, query_timeout: int = 120) -> Callable:
    """DB-API runner over the SQL analytics endpoint with an Entra access token (ODBC Driver 18), as fabric-backend
    connects. Returns execute(sql) -> list[dict]; execute.close() closes the connection."""
    import pyodbc
    tok = token.encode("utf-16-le")
    conn = pyodbc.connect(f"Driver={{ODBC Driver 18 for SQL Server}};Server={server};Database={database};Encrypt=yes",
                          attrs_before={1256: struct.pack("<i", len(tok)) + tok}, timeout=login_timeout)
    conn.timeout = query_timeout

    def execute(sql: str):
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    execute.close = conn.close
    return execute


def columns_sql(schema: str) -> str:
    lit = schema.replace("'", "''")
    return ("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_SCHEMA = '{lit}' ORDER BY TABLE_NAME, ORDINAL_POSITION")


class SqlTools:
    """Dispatches the two tools, counts statements and failures, tags every statement."""

    def __init__(self, execute: Callable, run_id: str) -> None:
        self.execute = execute
        self.run_id = run_id
        self.statements = 0
        self.failed = 0
        self.log: list[dict] = []
        self.tools = TOOLS

    def handles(self, name: str) -> bool:
        return name in ("list_tables", "run_sql")

    def call(self, name: str, args: dict, question_id: str, repeat: int) -> tuple[str, bool]:
        """(text for the model, is_error). An endpoint error goes back word for word."""
        if name == "list_tables":
            sql = columns_sql(str(args.get("schema") or "dbo"))
        else:
            try:
                sql = guard(str(args.get("query") or ""))
            except RefusedSQL as e:
                self.log.append({"question_id": question_id, "repeat": repeat, "sql": args.get("query"), "refused": str(e)})
                return f"refused: {e}", True
        tagged = tag(sql, self.run_id, question_id, repeat)
        self.statements += 1
        try:
            rows = self.execute(tagged)
        except Exception as e:  # noqa: BLE001 — the point is to show the model exactly what the endpoint said
            self.failed += 1
            self.log.append({"question_id": question_id, "repeat": repeat, "sql": tagged, "error": f"{type(e).__name__}: {e}"})
            return f"{type(e).__name__}: {e}", True
        self.log.append({"question_id": question_id, "repeat": repeat, "sql": tagged, "rows": len(rows)})
        if name == "list_tables":
            by_table: dict[str, list[str]] = {}
            for r in rows:
                r = {str(k).lower(): v for k, v in r.items()}
                by_table.setdefault(str(r["table_name"]), []).append(f"{r['column_name']} {r['data_type']}")
            text = "\n".join(f"{t}: {', '.join(cols)}" for t, cols in by_table.items()) or f"no tables in schema {args.get('schema')!r}"
            return text, False
        return render_rows(rows), False


def render_rows(rows: list[dict]) -> str:
    shown = rows[:MAX_ROWS]
    text = json.dumps({"row_count": len(rows), "rows": shown}, default=str)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f'… (truncated at {MAX_CHARS} characters; {len(rows)} rows in total)'
    elif len(rows) > MAX_ROWS:
        text += f" (first {MAX_ROWS} of {len(rows)} rows)"
    return text
