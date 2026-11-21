"""Check selection recall on controlled mutations of a pinned sqlglot checkout.

Runs full pytest for each mutation in a temporary copy; never changes the source
checkout. Collection errors, timeouts and unknown test IDs invalidate a case.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import selector  # noqa: E402


# These mutations are independent of the public sample answers.
CASES = {
    "fixture_metadata": ("tests/fixtures/optimizer/qualify_tables.sql", "# title: only information schema\n# dialect: bigquery", "# title: only information schema\n# dialect: postgres"),
    "fixture_sql": ("tests/fixtures/identity.sql", "SUM(1)\n", "SUM(2)\n"),
    "class_constant": ("sqlglot/dialects/postgres.py", "    INDEX_OFFSET = 1\n", "    INDEX_OFFSET = 0\n"),
    "class_schema": ("sqlglot/expressions/ddl.py", '        "constraint": False,\n', '        "constraint": True,\n'),
    "executor_function": ("sqlglot/executor/env.py", "    return None if value is None else not bool(value)\n", "    return None if value is None else bool(value)\n"),
    "context_filter": ("sqlglot/executor/context.py", "        rows = [reader.row for reader, _ in self if self.eval(condition)]\n", "        rows = [reader.row for reader, _ in self if not self.eval(condition)]\n"),
    "context_table": ("sqlglot/executor/context.py", "                if self._table.columns != other.columns:\n", "                if self._table.columns == other.columns:\n"),
    "context_range": ("sqlglot/executor/context.py", '        self.env["scope"] = self.range_readers\n', '        self.env["scope"] = self.row_readers\n'),
    "executor_substring": ("sqlglot/executor/env.py", "        start -= 1\n", "        start -= 2\n"),
    "optimizer_normalized": ("sqlglot/optimizer/normalize.py", "    ancestor, root = (exp.And, exp.Or) if dnf else (exp.Or, exp.And)\n", "    ancestor, root = (exp.Or, exp.And) if dnf else (exp.And, exp.Or)\n"),
    "expression_equality": ("sqlglot/expressions/core.py", "        return self is other or (type(self) is type(other) and hash(self) == hash(other))\n", "        return self is other and (type(self) is type(other) and hash(self) == hash(other))\n"),
    "dialect_normalization": ("sqlglot/dialects/dialect.py", "                expression.this.translate(ASCII_LOWER)\n", "                expression.this.translate(ASCII_UPPER)\n"),
    "optimizer_branch": ("sqlglot/optimizer/pushdown_predicates.py", "                    if position >= last_null_extending:\n", "                    if position > last_null_extending:\n"),
    "test_helper": ("tests/helpers.py", '        meta[key.lstrip("#").strip()] = val.strip()\n', '        meta[key.lstrip("#").strip()] = val.strip().upper()\n'),
    "snowflake_position": ("sqlglot/dialects/snowflake.py", "    SUPPORTS_POSITIONAL_COLUMN_REFS = True\n", "    SUPPORTS_POSITIONAL_COLUMN_REFS = False\n"),
    "snowflake_comments": ("sqlglot/dialects/snowflake.py", "        NESTED_COMMENTS = False\n", "        NESTED_COMMENTS = True\n"),
    "tokenizer_comments": ("sqlglot/tokens.py", "    NESTED_COMMENTS = True\n", "    NESTED_COMMENTS = False\n"),
    "schema_normalization": ("sqlglot/schema.py", "    if not normalize:\n        return identifier\n", "    if normalize:\n        return identifier\n"),
    "clickhouse_timestamp": ("sqlglot/generators/clickhouse.py", '        return self.func("fromUnixTimestamp64Milli", exp.cast(timestamp, exp.DType.BIGINT))\n', '        return self.func("fromUnixTimestamp64Micro", exp.cast(timestamp, exp.DType.BIGINT))\n'),
    "bigquery_week": ("sqlglot/parsers/bigquery.py", '        expr.set("unit", exp.WeekStart(this=exp.var("SUNDAY")))\n', '        expr.set("unit", exp.WeekStart(this=exp.var("MONDAY")))\n'),
    "helper_merge_ranges": ("sqlglot/helper.py", "        if start <= last_end:\n", "        if start < last_end:\n"),
    "tokenizer_qualify": ("sqlglot/tokens.py", '        "QUALIFY": TokenType.QUALIFY,\n', '        "QUALIFY": TokenType.WHERE,\n'),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    args = parser.parse_args()
    payload = json.loads((ROOT / "sample/tests.json").read_text())
    tests = payload["tests"]
    known = {item["id"] for item in tests}
    impact_map = selector.load_impact_map()
    failures = 0
    for name in args.case or CASES:
        path, before, after = CASES[name]
        with tempfile.TemporaryDirectory(prefix="rwx-mutation-") as temp:
            checkout = Path(temp) / "sqlglot"
            shutil.copytree(args.repo, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc", ".coverage*"))
            target = checkout / path
            original = target.read_text(encoding="utf-8")
            if original.count(before) != 1:
                raise ValueError(f"{name}: expected exactly one matching source fragment")
            changed = original.replace(before, after, 1)
            target.write_text(changed, encoding="utf-8")
            patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"))
            patch = f"diff --git a/{path} b/{path}\n" + patch
            selected = selector.select_test_ids(patch, tests, impact_map)
            selected_tests = [test for test in tests if test["id"] in selected]
            workers = selector.fill_idle_capacity(selector.schedule(selected_tests), tests)
            selected_numbers = {number for worker in workers for number in worker}
            final_selected = {test["id"] for test in tests if test["number"] in selected_numbers}
            report = Path(temp) / "report.xml"
            env = os.environ.copy()
            env.update({"SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                run = subprocess.run([args.python, "-m", "pytest", "-q", "--tb=no", f"--junitxml={report}"], cwd=checkout, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
            except subprocess.TimeoutExpired:
                print(json.dumps({"case": name, "error": "timeout"}), flush=True)
                failures += 1
                continue
            if run.returncode not in (0, 1) or not report.exists():
                print(json.dumps({"case": name, "error": "pytest did not complete", "output": run.stdout[-2000:]}), flush=True)
                failures += 1
                continue
            testcases = list(ET.parse(report).iter("testcase"))
            if len(testcases) != len(tests) + len(payload["skipped"]):
                print(json.dumps({"case": name, "error": "unexpected test count", "collected": len(testcases)}), flush=True)
                failures += 1
                continue
            failed = set()
            unknown = []
            unexpectedly_skipped = []
            for item in testcases:
                is_failed = item.find("failure") is not None or item.find("error") is not None
                is_skipped = item.find("skipped") is not None
                if not is_failed and not is_skipped:
                    continue
                parts = item.attrib.get("classname", "").split(".")
                if len(parts) < 2:
                    unknown.append(item.attrib)
                    continue
                test_id = "/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + item.attrib["name"]
                if is_skipped:
                    if test_id not in payload["skipped"]:
                        unexpectedly_skipped.append(test_id)
                    continue
                if test_id not in known:
                    unknown.append(test_id)
                else:
                    failed.add(test_id)
            missed = sorted(failed - final_selected)
            if unknown or unexpectedly_skipped or (run.returncode == 1 and not failed):
                failures += 1
            if missed:
                failures += 1
            print(json.dumps({"case": name, "failing": len(failed), "selected": len(final_selected), "missed_before_fill": sorted(failed - selected), "missed": missed, "unknown": unknown, "unexpectedly_skipped": unexpectedly_skipped}), flush=True)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
