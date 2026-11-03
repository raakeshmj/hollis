"""Data tools: exact answers over workspace tables, and a SQL surface that can only read."""

import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hollis.runtime.tools import Tool, ToolArgumentError, ToolContext, ToolError
from hollis.tools.builtin.data import MAX_RESULT_ROWS, data_tools, query_id

SALES = "region,amount,rep\nnorth,10,ana\nsouth,32.5,bo\nnorth,7,\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    (base / "data").mkdir(parents=True)
    (base / "data" / "sales.csv").write_text(SALES)
    (base / "data" / "people.jsonl").write_text('{"rep": "ana", "team": "a"}\n{"rep": "bo"}\n')
    (base / "data" / "targets.json").write_text('{"rows": [{"region": "north", "target": 20}]}')
    (tmp_path / "outside.csv").write_text("secret\nTOP-SECRET\n")
    return base


def _tools(root: Path, **kwargs: Any) -> dict[str, Tool]:
    return {t.descriptor.name: t for t in data_tools(root, **kwargs)}


async def _call(tool: Tool, invocation: str = "inv-1", **arguments: Any) -> str:
    context = ToolContext(run_id=uuid4(), invocation_id=invocation, attempt=1)
    output = await tool.invoke(tool.validate(arguments), context)
    text = "".join(getattr(p, "text", "") for p in output.parts)
    if output.is_error:
        raise ToolError(text)
    return text


async def test_list_and_describe(root: Path) -> None:
    tools = _tools(root)
    listing = await _call(tools["data_list"])
    assert "data/sales.csv\t" in listing
    assert "table sales" in listing
    described = await _call(tools["data_describe"], file="data/sales.csv", sample=2)
    assert "table sales: 3 rows, 3 columns" in described
    assert "- amount: real, 0 null, 3 distinct, min 7, max 32.5, mean 16.5" in described
    assert "- rep: text, 1 null, 2 distinct" in described


async def test_queries_are_exact_and_cite_a_stable_query_id(root: Path) -> None:
    sql = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region"
    out = await _call(_tools(root)["data_query"], sql=sql, files=["data/sales.csv"])
    assert out.splitlines()[0] == f"query_id: {query_id('inv-1')}"
    assert "| north | 17 |" in out
    assert "| south | 32.5 |" in out
    again = await _call(_tools(root)["data_query"], sql=sql, files=["data/sales.csv"])
    assert again.splitlines()[0] == out.splitlines()[0], "same invocation, same id (retries)"


async def test_joins_across_csv_json_and_jsonl(root: Path) -> None:
    sql = (
        "SELECT s.region, SUM(s.amount) AS total, t.target FROM sales s "
        "JOIN targets t ON t.region = s.region GROUP BY s.region"
    )
    out = await _call(
        _tools(root)["data_query"], sql=sql, files=["data/sales.csv", "data/targets.json"]
    )
    assert "| north | 17 | 20 |" in out


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) DELETE FROM sales",
        "WITH x AS (SELECT 1) INSERT INTO sales VALUES ('east', 1, 'cy')",
        "WITH x AS (SELECT 1) UPDATE sales SET amount = 0",
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM pragma_table_info('sales')",
        "SELECT load_extension('/tmp/evil')",
        "SELECT fts3_tokenizer('simple')",  # answers a native pointer when allowed
        "SELECT readfile('/etc/passwd')",
    ],
)
async def test_the_sql_surface_can_only_read(root: Path, sql: str) -> None:
    with pytest.raises(ToolError, match="SQL error"):
        await _call(_tools(root)["data_query"], sql=sql, files=["data/sales.csv"])


@pytest.mark.parametrize(
    "sql",
    ["DROP TABLE sales", "SELECT 1; DROP TABLE sales", "ATTACH '/tmp/x.db' AS x", "VALUES (1)"],
)
async def test_only_one_select_statement(root: Path, sql: str) -> None:
    with pytest.raises(ToolError, match="one SELECT"):
        await _call(_tools(root)["data_query"], sql=sql, files=["data/sales.csv"])


async def test_values_are_capped_in_size(root: Path) -> None:
    with pytest.raises(ToolError, match="too big"):
        await _call(
            _tools(root)["data_query"], sql="SELECT zeroblob(5000000)", files=["data/sales.csv"]
        )


async def test_runaway_queries_are_stopped(root: Path) -> None:
    sql = "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT x FROM r"
    started = time.monotonic()
    with pytest.raises(ToolError, match=r"longer than 0\.3s"):
        await _call(
            _tools(root, query_seconds=0.3)["data_query"], sql=sql, files=["data/sales.csv"]
        )
    assert time.monotonic() - started < 5


async def test_results_are_capped_in_rows(root: Path) -> None:
    sql = (
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r LIMIT 1000) SELECT x FROM r"
    )
    out = await _call(_tools(root)["data_query"], sql=sql, files=["data/sales.csv"])
    assert f"rows: 1000 (showing {MAX_RESULT_ROWS})" in out
    assert "800 more rows not shown" in out


@pytest.mark.parametrize("path", ["../outside.csv", "/etc/hosts", ".git/data.csv"])
async def test_files_stay_inside_the_root(root: Path, path: str) -> None:
    with pytest.raises(ToolError, match=r"escapes|absolute|protected"):
        await _call(_tools(root)["data_query"], sql="SELECT * FROM outside", files=[path])


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_links_out_of_the_root_are_refused(root: Path) -> None:
    os.symlink(root.parent / "outside.csv", root / "data" / "link.csv")
    with pytest.raises(ToolError, match="escapes"):
        await _call(_tools(root)["data_query"], sql="SELECT * FROM link", files=["data/link.csv"])
    assert "link.csv" not in await _call(_tools(root)["data_list"])


async def test_malformed_files_are_reported_not_crashed(root: Path) -> None:
    (root / "data" / "wide.csv").write_text('a\n"' + "x" * 200_000 + '"\n')
    (root / "data" / "broken.json").write_text("[{")
    with pytest.raises(ToolError, match="could not be read"):
        await _call(_tools(root)["data_describe"], file="data/wide.csv")
    with pytest.raises(ToolError, match="could not be read"):
        await _call(_tools(root)["data_describe"], file="data/broken.json")


def test_query_arguments_are_bounded(root: Path) -> None:
    tool = _tools(root)["data_query"]
    with pytest.raises(ToolArgumentError):
        tool.validate({"sql": "SELECT 1", "files": []})
    with pytest.raises(ToolArgumentError):
        tool.validate({"sql": "SELECT 1", "files": ["x" * 2000]})
