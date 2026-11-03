"""Data tools confined to one root directory (M8): find, describe and query tabular files.

Answers are computed exactly, not estimated by a model reading rows:
- `data_list` finds CSV, TSV, JSON and JSON Lines files.
- `data_describe` gives a table's columns, inferred types, null and distinct counts, numeric
  ranges and means, and a few sample rows.
- `data_query` runs one read-only SQL query (SQLite dialect) over the files it names, each loaded
  as a table named after the file. Every result carries a `query_id`, derived from the tool
  invocation, so a report can say which query produced each number, and Hollis can check it.

Queries run in an in-memory SQLite database that holds only the loaded copies, so a query can't
reach any file. An authorizer allows reading and functions only (no writes, ATTACH, PRAGMA or
schema changes), extensions can't be loaded, values are capped in size, a progress handler stops
queries that run too long, and results are capped in rows and characters.
"""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, cast

from pydantic import Field

from hollis.runtime.tools import Tool, ToolContext, ToolEffect, ToolError
from hollis.tools.builtin.code import SKIP_DIRS
from hollis.tools.builtin.filesystem import resolve_within
from hollis.tools.function import FunctionTool

SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".ndjson")
MAX_FILE_BYTES = 50_000_000
MAX_ROWS = 500_000
MAX_RESULT_ROWS = 200
MAX_CELL_CHARS = 200
QUERY_SECONDS = 5.0
MAX_VALUE_BYTES = 1_000_000
"""The largest string or blob a query may build (zeroblob, printf, group_concat...)."""
_CACHE_SIZE = 8
_TABLE = re.compile(r"[^A-Za-z0-9_]")
_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}

RelPath = Annotated[
    str, Field(min_length=1, max_length=1024, description="Path relative to the root")
]


_DENIED_FUNCTIONS = frozenset({"fts3_tokenizer", "fts5", "load_extension"})
"""Functions that expose or accept native pointers, or load code."""


def _authorize(action: int, *details: str | None) -> int:
    """Allow reading and functions only: no writes, ATTACH, PRAGMA or schema changes, and none
    of the functions that deal in native pointers or load code."""
    if action == sqlite3.SQLITE_FUNCTION and (details[1] or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def query_id(invocation_id: str) -> str:
    """The id `data_query` gives a result: stable for an invocation, across attempts."""
    return "Q-" + hashlib.sha256(invocation_id.encode()).hexdigest()[:8]


def table_name(path: str) -> str:
    name = _TABLE.sub("_", Path(path).stem) or "data"
    return f"t_{name}" if name[0].isdigit() else name


class _Table:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows


def _convert(value: str) -> Any:
    text = value.strip()
    if text == "":
        return None
    if re.fullmatch(r"[+-]?\d+", text) and len(text.lstrip("+-")) < 19:
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", text):
        return float(text)
    return value


def _cell(value: Any) -> Any:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def _load(path: Path) -> _Table:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
    try:
        return _parse(path)
    except (csv.Error, json.JSONDecodeError, RecursionError) as exc:
        raise ToolError(f"{path.name} could not be read: {exc}") from None


def _parse(path: Path) -> _Table:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        reader = csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ",")
        header = next(reader, None)
        if not header:
            raise ToolError(f"{path.name} is empty")
        columns = _unique([h.strip() or f"column_{i + 1}" for i, h in enumerate(header)])
        rows: list[list[Any]] = []
        for record in reader:
            if len(rows) >= MAX_ROWS:
                raise ToolError(f"{path.name} has more than {MAX_ROWS} rows")
            padded = (record + [""] * len(columns))[: len(columns)]
            rows.append([_convert(v) for v in padded])
        return _Table(columns, rows)
    if suffix == ".json":
        data: object = json.loads(text)
        if isinstance(data, dict) and len(cast(dict[str, Any], data)) == 1:
            data = next(iter(cast(dict[str, Any], data).values()))
        records = cast(list[Any], data) if isinstance(data, list) else None
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if records is None or not all(isinstance(r, dict) for r in records):
        raise ToolError(f"{path.name}: expected a list of JSON objects")
    if len(records) > MAX_ROWS:
        raise ToolError(f"{path.name} has more than {MAX_ROWS} rows")
    columns = _unique(list(dict.fromkeys(k for r in records for k in cast(dict[str, Any], r))))
    rows = [[_cell(cast(dict[str, Any], r).get(c)) for c in columns] for r in records]
    return _Table(columns, rows)


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        count = seen.get(name, 0)
        seen[name] = count + 1
        unique.append(name if count == 0 else f"{name}_{count + 1}")
    return unique


def _render(columns: list[str], rows: list[tuple[Any, ...]], total: int) -> str:
    def cell(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, float):
            return repr(round(value, 10))
        return str(value).replace("|", "\\|").replace("\n", " ")[:MAX_CELL_CHARS]

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    if total > len(rows):
        lines.append(f"[{total - len(rows)} more rows not shown; aggregate or add LIMIT]")
    return "\n".join(lines)


def data_tools(
    root: str | os.PathLike[str], *, prefix: str = "data", query_seconds: float = QUERY_SECONDS
) -> list[Tool]:
    base = Path(root)
    real_root = base.resolve(strict=True)
    if not real_root.is_dir():
        raise ValueError(f"{root} is not a directory")
    cache: OrderedDict[tuple[str, int, int], _Table] = OrderedDict()

    def table(relative: str) -> tuple[str, _Table]:
        path = resolve_within(base, relative)
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            raise ToolError(f"{relative} is not a CSV, TSV, JSON or JSON Lines file")
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
        if key not in cache:
            cache[key] = _load(path)
            while len(cache) > _CACHE_SIZE:
                cache.popitem(last=False)
        cache.move_to_end(key)
        return table_name(relative), cache[key]

    def connect(files: list[str]) -> tuple[sqlite3.Connection, list[str]]:
        connection = sqlite3.connect(":memory:")
        names: list[str] = []
        for relative in dict.fromkeys(files):
            name, loaded = table(relative)
            if name in names:
                raise ToolError(f"two files would both be table {name!r}; query them separately")
            names.append(name)
            quoted = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}"' for c in loaded.columns)
            connection.execute(f'CREATE TABLE "{name}" ({quoted})')
            marks = ", ".join("?" * len(loaded.columns))
            connection.executemany(f'INSERT INTO "{name}" VALUES ({marks})', loaded.rows)  # noqa: S608  # quoted identifiers
        connection.execute("PRAGMA query_only = ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        connection.set_authorizer(_authorize)
        return connection, names

    def run(
        connection: sqlite3.Connection, sql: str
    ) -> tuple[list[str], list[tuple[Any, ...]], int]:
        deadline = time.monotonic() + query_seconds
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cursor = connection.execute(sql)
            columns = [d[0] for d in cursor.description or ()]
            rows = cursor.fetchmany(MAX_RESULT_ROWS)
            total = len(rows) + sum(1 for _ in cursor)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise ToolError(f"the query ran longer than {query_seconds:g}s") from None
            raise ToolError(f"SQL error: {exc}") from None
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from None
        return columns, rows, total

    def list_files(path: RelPath = ".") -> str:
        """Find tabular data files (CSV, TSV, JSON, JSON Lines) under a directory."""
        start = resolve_within(base, path)
        if not start.is_dir():
            raise ToolError(f"{path} is not a directory")
        found: list[str] = []
        for directory, dirs, names in os.walk(start):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                file = Path(directory) / name
                if file.suffix.lower() in SUFFIXES and not file.is_symlink():
                    relative = file.relative_to(real_root).as_posix()
                    found.append(
                        f"{relative}\t{file.stat().st_size} bytes\ttable {table_name(relative)}"
                    )
                    if len(found) >= 200:
                        return "\n".join([*found, "[more files not shown]"])
        return "\n".join(found) or "no tabular files found"

    def describe(
        file: RelPath,
        sample: Annotated[int, Field(ge=0, le=20, description="Rows to show")] = 5,
    ) -> str:
        """Describe a data file: columns, types, nulls, distinct values, ranges, sample rows."""
        name, loaded = table(file)
        connection, _ = connect([file])
        try:
            lines = [f"table {name}: {len(loaded.rows)} rows, {len(loaded.columns)} columns"]
            for column in loaded.columns:
                values = [row[loaded.columns.index(column)] for row in loaded.rows]
                present = [v for v in values if v is not None]
                numeric = all(isinstance(v, int | float) for v in present) and bool(present)
                kind = (
                    "integer" if numeric and all(isinstance(v, int) for v in present)
                    else "real" if numeric else "text"
                )  # fmt: skip
                facts = [
                    kind,
                    f"{len(values) - len(present)} null",
                    f"{len(set(present))} distinct",
                ]
                if numeric:
                    numbers = cast(list[float], present)
                    mean = sum(numbers) / len(numbers)
                    facts.append(f"min {min(numbers)}, max {max(numbers)}, mean {round(mean, 6)}")
                lines.append(f"- {column}: " + ", ".join(facts))
            if sample:
                sql = f'SELECT * FROM "{name}" LIMIT {int(sample)}'  # noqa: S608  # quoted identifier
                columns, rows, total = run(connection, sql)
                lines += ["sample:", _render(columns, rows, min(total, sample))]
            return "\n".join(lines)
        finally:
            connection.close()

    def query(
        sql: Annotated[
            str, Field(min_length=1, max_length=10_000, description="One SELECT (SQLite)")
        ],
        files: Annotated[
            list[RelPath],
            Field(
                min_length=1,
                max_length=8,
                description="Data files to query; each is a table named after the file",
            ),
        ],
        context: ToolContext,
    ) -> str:
        """Run one read-only SQL query over data files. Answers exact results and a query_id
        to cite for each number you report."""
        statement = sql.strip().rstrip(";").strip()
        if ";" in statement or not re.match(r"(?is)^\s*(select|with)\b", statement):
            raise ToolError("one SELECT (or WITH ... SELECT) statement only")
        connection, names = connect(files)
        try:
            columns, rows, total = run(connection, statement)
        finally:
            connection.close()
        header = [
            f"query_id: {query_id(context.invocation_id)}",
            f"tables: {', '.join(names)}",
            f"rows: {total}" + (f" (showing {len(rows)})" if total > len(rows) else ""),
        ]
        return "\n".join([*header, _render(columns, rows, total)])

    return [
        FunctionTool(list_files, name=f"{prefix}_list", effect=ToolEffect.READ_ONLY),
        FunctionTool(describe, name=f"{prefix}_describe", effect=ToolEffect.READ_ONLY),
        FunctionTool(query, name=f"{prefix}_query", effect=ToolEffect.READ_ONLY, timeout_s=30),
    ]
