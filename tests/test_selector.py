from __future__ import annotations

import json
import os
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

    def test_sample_is_valid_zero_miss_and_below_target(self) -> None:
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
        self.assertLess(simulated_seconds, 160.0)
        self.assertIn(b"selected", result.stderr)


if __name__ == "__main__":
    unittest.main()
