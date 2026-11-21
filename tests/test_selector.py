from __future__ import annotations

import json
import itertools
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selector  # noqa: E402


class PatchTests(unittest.TestCase):
    def test_multifile_comments_are_classified_per_file(self) -> None:
        patch = """\
diff --git a/sqlglot/a.py b/sqlglot/a.py
--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -1 +1,2 @@
 x = 1
+# harmless
diff --git a/sqlglot/b.py b/sqlglot/b.py
--- a/sqlglot/b.py
+++ b/sqlglot/b.py
@@ -4 +4 @@
-value = True
+value = False
"""
        changes = selector.parse_patch(patch)
        self.assertEqual([True, False], [selector.is_comment_only(change) for change in changes])

    def test_control_flow_mutation(self) -> None:
        change = selector.ChangedFile(
            "sqlglot/optimizer/example.py", added=["or ready"], removed=["and ready"]
        )
        self.assertTrue(selector.is_control_flow_mutation(change))

    def test_diff_content_starting_with_header_characters_is_not_lost(self) -> None:
        patch = """\
diff --git a/tests/fixtures/query.sql b/tests/fixtures/query.sql
--- a/tests/fixtures/query.sql
+++ b/tests/fixtures/query.sql
@@ -1 +1 @@
--- old content
+++ new content
"""
        change = selector.parse_patch(patch)[0]
        self.assertEqual(["-- old content"], change.removed)
        self.assertEqual(["++ new content"], change.added)

    def test_insert_at_function_boundary_checks_both_sides(self) -> None:
        patch = """\
diff --git a/sqlglot/example.py b/sqlglot/example.py
--- a/sqlglot/example.py
+++ b/sqlglot/example.py
@@ -4,2 +4,3 @@
     return 1
+    do_something()
 def other():
"""
        change = selector.parse_patch(patch)[0]
        self.assertEqual({4, 5}, change.points)

    def test_hash_inside_python_multiline_string_is_not_a_comment(self) -> None:
        change = selector.ChangedFile("sqlglot/example.py", points={11}, added=["# new"], removed=["# old"])
        self.assertFalse(selector.is_comment_only(change, {"strings": [[9, 12]]}))
        self.assertTrue(selector.is_comment_only(change, {"strings": [[11, 12]]}))


class SchedulerTests(unittest.TestCase):
    def test_empty_selection_runs_nothing(self) -> None:
        self.assertEqual([], selector.schedule([]))

    def test_scheduler_is_valid_and_unique(self) -> None:
        tests = [
            {"number": 0, "seconds": 8.0},
            {"number": 1, "seconds": 5.0},
            {"number": 2, "seconds": 3.0},
        ]
        workers = selector.schedule(tests)
        flattened = [number for worker in workers for number in worker]
        self.assertLessEqual(len(workers), 8)
        self.assertEqual([0, 1, 2], sorted(flattened))
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_two_worker_partition_beats_greedy_without_skipping_tests(self) -> None:
        durations = [2.2, 1.6, 1.4, 1.2, 1.9]
        tests = [{"number": n, "seconds": seconds} for n, seconds in enumerate(durations)]
        workers = selector.schedule(tests)
        self.assertEqual(2, len(workers))
        self.assertEqual(list(range(5)), sorted(n for worker in workers for n in worker))
        load_ms = max(sum(round(durations[n] * 1000) for n in worker) for worker in workers)
        self.assertEqual(8200, load_ms + 2000 * len(workers))

    def test_scheduler_matches_or_beats_exhaustive_two_worker_cost(self) -> None:
        rng = random.Random(71)
        for _ in range(40):
            durations = [rng.randrange(0, 6000) for _ in range(rng.randrange(2, 9))]
            tests = [{"number": i, "seconds": ms / 1000} for i, ms in enumerate(durations)]
            workers = selector.schedule(tests)
            self.assertEqual(list(range(len(tests))), sorted(n for w in workers for n in w))
            actual = max(sum(durations[n] for n in w) for w in workers) + 2000 * len(workers)
            two_worker_best = min(
                max(sum(d for d, side in zip(durations, bits) if side),
                    sum(d for d, side in zip(durations, bits) if not side)) + 4000
                for bits in itertools.product((False, True), repeat=len(tests))
                if any(bits) and not all(bits)
            )
            self.assertLessEqual(actual, two_worker_best)

    def test_fill_idle_capacity_adds_tests_at_no_simulated_cost(self) -> None:
        durations = [10.0, 2.0, 3.0, 4.0, 11.0]
        tests = [{"number": i, "seconds": value} for i, value in enumerate(durations)]
        workers = [[0], [1]]
        filled = selector.fill_idle_capacity(workers, tests)
        numbers = [number for worker in filled for number in worker]
        self.assertEqual({0, 1, 2, 3}, set(numbers))
        self.assertEqual(len(numbers), len(set(numbers)))
        self.assertEqual(10.0, max(sum(durations[n] for n in w) for w in filled))
        self.assertEqual([], selector.fill_idle_capacity([], tests))

    def test_idle_fill_preserves_selected_tests_and_worker_cost(self) -> None:
        rng = random.Random(82)
        for _ in range(80):
            tests = [{"number": i, "seconds": rng.randrange(0, 12000) / 1000} for i in range(16)]
            initial = selector.schedule(tests[:rng.randrange(1, len(tests))])
            before = {n for worker in initial for n in worker}
            cost = lambda workers: max(sum(round(tests[n]["seconds"] * 1000) for n in w) for w in workers) + 2000 * len(workers)
            before_cost = cost(initial)
            filled = selector.fill_idle_capacity(initial, tests)
            flat = [n for worker in filled for n in worker]
            self.assertTrue(before.issubset(flat))
            self.assertEqual(len(flat), len(set(flat)))
            self.assertEqual(before_cost, cost(filled))


class SampleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
        cls.impact_map = selector.load_impact_map(ROOT / "impact_map.json.gz")

    def select(self, patch: str) -> set[str]:
        return selector.select_test_ids(patch, self.tests, self.impact_map)

    def test_root_fixture_has_explicit_owners(self) -> None:
        patch = """\
diff --git a/tests/fixtures/identity.sql b/tests/fixtures/identity.sql
--- a/tests/fixtures/identity.sql
+++ b/tests/fixtures/identity.sql
@@ -1 +1 @@
-SELECT 1;
+SELECT 2;
"""
        selected = self.select(patch)
        self.assertIn("tests/test_serde.py::TestSerde::test_serde", selected)
        self.assertIn("tests/test_transpile.py::TestTranspile::test_identity", selected)

    def test_fixture_metadata_is_not_a_python_comment(self) -> None:
        patch = """\
diff --git a/tests/fixtures/optimizer/qualify_tables.sql b/tests/fixtures/optimizer/qualify_tables.sql
--- a/tests/fixtures/optimizer/qualify_tables.sql
+++ b/tests/fixtures/optimizer/qualify_tables.sql
@@ -18 +18 @@
-# dialect: bigquery
+# dialect: postgres
"""
        selected = self.select(patch)
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_qualify_tables", selected)

    def test_class_constant_uses_runtime_class_owners(self) -> None:
        patch = """\
diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py
--- a/sqlglot/expressions/ddl.py
+++ b/sqlglot/expressions/ddl.py
@@ -82 +82 @@ class TriggerProperties(Expression):
-        \"constraint\": False,
+        \"constraint\": True,
"""
        selected = self.select(patch)
        self.assertIn(
            "tests/dialects/test_postgres.py::TestPostgres::test_postgres_create_trigger",
            selected,
        )

    def test_shared_test_helper_uses_coverage_owners(self) -> None:
        patch = """\
diff --git a/tests/helpers.py b/tests/helpers.py
--- a/tests/helpers.py
+++ b/tests/helpers.py
@@ -35 +35 @@ def load_sql_fixtures(filename):
-    with open(path) as f:
+    with open(path, encoding=\"utf-8\") as f:
"""
        selected = self.select(patch)
        self.assertIn("tests/test_transpile.py::TestTranspile::test_identity", selected)

    def test_imported_module_global_has_cross_module_owners(self) -> None:
        patch = """\
diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py
--- a/sqlglot/executor/env.py
+++ b/sqlglot/executor/env.py
@@ -249 +249 @@
-ENV = {
+ENV = dict({
"""
        selected = self.select(patch)
        self.assertIn(
            "tests/test_executor.py::TestExecutor::test_execute_subqueries",
            selected,
        )

    def test_executor_changes_include_full_integration_suite(self) -> None:
        patch = """\
diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py
--- a/sqlglot/executor/env.py
+++ b/sqlglot/executor/env.py
@@ -13 +13 @@
-    return None if value is None else not bool(value)
+    return None if value is None else bool(value)
"""
        selected = self.select(patch)
        self.assertIn("tests/test_executor.py::TestExecutor::test_execute_tpcds", selected)

    def test_dialect_normalization_keeps_optimizer_branch_tests(self) -> None:
        patch = """\
diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py
--- a/sqlglot/dialects/dialect.py
+++ b/sqlglot/dialects/dialect.py
@@ -1261 +1261 @@
-                expression.this.translate(ASCII_LOWER)
+                expression.this.translate(ASCII_UPPER)
"""
        selected = self.select(patch)
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_merge_subqueries", selected)
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_pushdown_projection", selected)

    def test_parser_changes_keep_optimizer_simplify(self) -> None:
        patch = """\
diff --git a/sqlglot/parsers/bigquery.py b/sqlglot/parsers/bigquery.py
--- a/sqlglot/parsers/bigquery.py
+++ b/sqlglot/parsers/bigquery.py
@@ -35 +35 @@
-        expr.set("unit", exp.WeekStart(this=exp.var("SUNDAY")))
+        expr.set("unit", exp.WeekStart(this=exp.var("MONDAY")))
"""
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_simplify", self.select(patch))

    def test_schema_normalization_keeps_optimizer_branch_tests(self) -> None:
        patch = """\
diff --git a/sqlglot/schema.py b/sqlglot/schema.py
--- a/sqlglot/schema.py
+++ b/sqlglot/schema.py
@@ -719 +719 @@
-    if not normalize:
+    if normalize:
"""
        selected = self.select(patch)
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_optimize", selected)
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_pushdown_projection", selected)

    def test_merge_ranges_keeps_simplify(self) -> None:
        patch = """\
diff --git a/sqlglot/helper.py b/sqlglot/helper.py
--- a/sqlglot/helper.py
+++ b/sqlglot/helper.py
@@ -441 +441 @@
-        if start <= last_end:
+        if start < last_end:
"""
        self.assertIn("tests/test_optimizer.py::TestOptimizer::test_simplify", self.select(patch))

    def test_tokenizer_table_change_runs_full_suite(self) -> None:
        patch = """\
diff --git a/sqlglot/tokens.py b/sqlglot/tokens.py
--- a/sqlglot/tokens.py
+++ b/sqlglot/tokens.py
@@ -377 +377 @@
-        "QUALIFY": TokenType.QUALIFY,
+        "QUALIFY": TokenType.WHERE,
"""
        self.assertEqual({test["id"] for test in self.tests}, self.select(patch))

    def test_spawned_process_coverage_distinguishes_headers_from_bodies(self) -> None:
        test_id = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
        index = self.impact_map["test_ids"].index(test_id)
        files = self.impact_map["files"]
        context = files["sqlglot/executor/context.py"]
        optimizer = files["sqlglot/optimizer/eliminate_subqueries.py"]
        self.assertIn(index, selector._scope_ids(context, 54))
        self.assertIn(index, selector._scope_ids(optimizer, 157))
        self.assertNotIn(index, selector._scope_ids(optimizer, 176))

    def test_unknown_fixture_is_not_silently_ignored(self) -> None:
        patch = """\
diff --git a/tests/fixtures/unknown/data.json b/tests/fixtures/unknown/data.json
--- a/tests/fixtures/unknown/data.json
+++ b/tests/fixtures/unknown/data.json
@@ -1 +1 @@
-{}
+{"x": 1}
"""
        self.assertEqual({test["id"] for test in self.tests}, self.select(patch))

    def test_unmapped_code_is_not_silently_ignored(self) -> None:
        patch = """\
diff --git a/sqlglot/new_module.py b/sqlglot/new_module.py
--- a/sqlglot/new_module.py
+++ b/sqlglot/new_module.py
@@ -0,0 +1 @@
+value = 1
"""
        self.assertEqual({test["id"] for test in self.tests}, self.select(patch))

    def test_sample_is_valid_and_has_zero_misses(self) -> None:
        sample = ROOT / "sample"
        if not sample.exists():
            self.skipTest("sample kit not included")

        environment = os.environ.copy()
        environment["BLAST_RADIUS_DATA"] = str(sample)
        with tempfile.NamedTemporaryFile() as output:
            result = subprocess.run(
                [sys.executable, str(ROOT / "selector.py")],
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
            )
            output.flush()
            report = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "score.py"),
                    str(sample),
                    str(sample / "answers.json"),
                    output.name,
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        self.assertIn("Misses:    0", report)
        simulated_line = next(line for line in report.splitlines() if line.startswith("Simulated:"))
        simulated_seconds = float(simulated_line.split()[1])
        # Includes the full executor integration suite for an executor change.
        self.assertLess(simulated_seconds, 250.0)
        self.assertIn(b"selected", result.stderr)

    def test_fast_entry_point_keeps_the_reliable_selector(self) -> None:
        sample = ROOT / "sample"
        environment = os.environ.copy()
        environment["BLAST_RADIUS_DATA"] = str(sample)
        with tempfile.NamedTemporaryFile() as output:
            subprocess.run([sys.executable, str(ROOT / "selector_fast.py")], env=environment, stdout=output, stderr=subprocess.PIPE, check=True)
            output.flush()
            safe = subprocess.run([sys.executable, str(ROOT / "selector.py")], env=environment, capture_output=True, check=True).stdout
            self.assertEqual(Path(output.name).read_bytes(), safe)
            report = subprocess.run([sys.executable, str(ROOT / "score.py"), str(sample), str(sample / "answers.json"), output.name], capture_output=True, text=True, check=True).stdout
        self.assertIn("Misses:    0", report)
        simulated = float(next(line for line in report.splitlines() if line.startswith("Simulated:")).split()[1])
        self.assertLess(simulated, 250.0)


if __name__ == "__main__":
    unittest.main()
