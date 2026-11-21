"""Static blast-radius selector for the RWX sqlglot challenge.

Runtime code uses only the Python standard library.  The coverage map is built
from the pinned, unmodified sqlglot checkout; it contains no mutation answers.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BLAST_RADIUS_DATA", "/data"))
MAP_PATH = Path(os.environ.get("BLAST_RADIUS_MAP", APP_DIR / "impact_map.json.gz"))
MAX_WORKERS = 8
WORKER_COST_MS = 2_000
SMALL_FILE_CONTEXT_LIMIT = 40


@dataclass
class ChangedFile:
    """The old-tree locations and changed text for one file in a patch."""

    path: str
    points: set[int] = field(default_factory=set)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def parse_patch(text: str) -> list[ChangedFile]:
    """Parse just enough unified-diff structure for safe static routing."""

    changes: list[ChangedFile] = []
    current: ChangedFile | None = None
    old_path = ""
    old_line: int | None = None

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                changes.append(current)
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            old_path = match.group(1) if match else ""
            current = ChangedFile(match.group(2) if match else "")
            old_line = None
        elif current is not None and old_line is None and line.startswith("--- "):
            if line.startswith("--- a/"):
                old_path = line[6:]
        elif current is not None and old_line is None and line.startswith("+++ "):
            if line.startswith("+++ b/"):
                current.path = line[6:]
            elif line == "+++ /dev/null":
                current.path = old_path
        elif current is not None and line.startswith("@@"):
            match = re.search(r"-(\d+)", line)
            old_line = int(match.group(1)) if match else None
        elif current is not None and old_line is not None:
            if line.startswith("-"):
                current.points.add(old_line)
                current.removed.append(line[1:].strip())
                old_line += 1
            elif line.startswith("+"):
                # An insertion is associated with the following old-tree line.
                current.points.add(old_line)
                current.added.append(line[1:].strip())
            elif line.startswith(" "):
                old_line += 1

    if current is not None:
        changes.append(current)
    return [change for change in changes if change.path]


def is_comment_only(change: ChangedFile, file_map: dict | None = None) -> bool:
    """Recognize Python-style comment/blank-only edits independently per file."""

    changed = change.added + change.removed
    # SQL fixture lines such as "# dialect: bigquery" are parsed as metadata.
    # A leading hash is not generally a comment outside Python source files.
    if file_map and any(
        start < point <= end
        for start, end in file_map.get("strings", [])
        for point in change.points
    ):
        return False
    return change.path.endswith(".py") and bool(changed) and all(
        not line or line.startswith("#") for line in changed
    )


def is_control_flow_mutation(change: ChangedFile) -> bool:
    """Flag mutations likely to fan out into slow optimizer integration tests."""

    removed = set(re.findall(r"\b(?:and|or|not)\b|==|!=|<=|>=|<|>", " ".join(change.removed)))
    added = set(re.findall(r"\b(?:and|or|not)\b|==|!=|<=|>=|<|>", " ".join(change.added)))
    return removed != added and bool(removed or added)


def _tests_in_file(tests: list[dict], path: str) -> set[str]:
    return {test["id"] for test in tests if test["file"] == path}


def _scope_ids(file_map: dict, line: int) -> set[int]:
    candidates = [
        scope
        for scope in file_map.get("scopes", [])
        if scope[0] <= line <= scope[1]
    ]
    if not candidates:
        return set()
    smallest = min(candidates, key=lambda scope: scope[1] - scope[0])
    return set(smallest[2])


def _class_ids(file_map: dict, line: int) -> set[int]:
    """Return tests that instantiated the smallest enclosing runtime class."""

    candidates = [
        scope
        for scope in file_map.get("classes", [])
        if scope[0] <= line <= scope[1]
    ]
    if not candidates:
        return set()
    smallest = min(candidates, key=lambda scope: scope[1] - scope[0])
    return set(smallest[2])


def _global_ids(file_map: dict, line: int) -> set[int]:
    for scope in file_map.get("globals", []):
        if scope[0] <= line <= scope[1]:
            return set(scope[2])
    return set()


def _fixture_fallback_ids(path: str, tests: list[dict]) -> set[str]:
    """Route a new/unmapped fixture using its directory and normalized stem."""

    parts = Path(path).parts
    group = parts[2].lower() if len(parts) > 2 else ""
    stem = Path(path).stem.lower()
    candidates = {stem, stem.rstrip("s")}
    if stem.endswith("ies"):
        candidates.add(f"{stem[:-3]}y")

    selected: set[str] = set()
    for test in tests:
        method = test["id"].rsplit("::", 1)[-1].removeprefix("test_").lower()
        if group == "optimizer":
            if test["file"] == "tests/test_optimizer.py" and any(
                method == candidate
                or method.startswith(f"{candidate}__")
                or candidate.startswith(f"{method}_")
                for candidate in candidates
            ):
                selected.add(test["id"])
        elif group and test["file"] == f"tests/dialects/test_{group}.py":
            selected.add(test["id"])
    return selected


def select_test_ids(
    patch_text: str,
    tests: list[dict],
    impact_map: dict,
) -> set[str]:
    """Select test IDs for one patch without executing sqlglot."""

    selected_indices: set[int] = set()
    selected_ids: set[str] = set()
    map_test_ids: list[str] = impact_map["test_ids"]
    files: dict = impact_map.get("files", {})
    fixture_map: dict = impact_map.get("fixtures", {})
    dialect_fixture_refs: dict = impact_map.get("dialect_fixture_refs", {})
    known_ids = {test["id"] for test in tests}

    def add_known(*ids: str) -> None:
        selected_ids.update(test_id for test_id in ids if test_id in known_ids)

    for change in parse_patch(patch_text):
        path = change.path
        if is_comment_only(change, files.get(path)):
            continue

        if path.startswith("tests/fixtures/"):
            mapped = fixture_map.get(path)
            if mapped is None:
                fallback = _fixture_fallback_ids(path, tests)
                selected_ids.update(fallback)
                if not fallback and Path(path).suffix in {".sql", ".json", ".csv", ".txt"}:
                    selected_ids.update(known_ids)
            else:
                selected_indices.update(mapped)
            continue

        if path.startswith("tests/"):
            direct_tests = _tests_in_file(tests, path)
            if direct_tests:
                selected_ids.update(direct_tests)
                continue

        if not path.startswith("sqlglot/") and path not in files:
            if (path.startswith("tests/") and Path(path).suffix in {".py", ".sql", ".json", ".csv", ".txt"}) or path in {"pyproject.toml", "setup.cfg", "pytest.ini", "conftest.py"}:
                selected_ids.update(known_ids)
            continue

        file_map = files.get(path, {})
        has_unowned_point = not change.points
        for point in change.points:
            owners = (
                _scope_ids(file_map, point)
                or _class_ids(file_map, point)
                or _global_ids(file_map, point)
            )
            selected_indices.update(owners)
            has_unowned_point = has_unowned_point or not owners

        all_file_indices = file_map.get("all", [])
        if len(all_file_indices) <= SMALL_FILE_CONTEXT_LIMIT:
            selected_indices.update(all_file_indices)
            if all_file_indices:
                has_unowned_point = False

        # An uncovered branch or new symbol can still affect existing callers.
        # Retain every observed test for the file when no narrower owner exists.
        if has_unowned_point:
            if all_file_indices:
                selected_indices.update(all_file_indices)
            elif path.endswith(".py"):
                selected_ids.update(known_ids)

        parts = Path(path).parts
        family = parts[1] if len(parts) > 2 else ""
        stem = Path(path).stem

        # Top-level modules have direct test files but no family-specific rule.
        # This primarily protects module constants and import-time registries,
        # which per-test coverage cannot observe during collection.
        if len(parts) == 2 and has_unowned_point:
            selected_ids.update(_tests_in_file(tests, f"tests/test_{stem}.py"))

        if family in {"dialects", "parsers", "generators"} and stem not in {
            "__init__",
            "dialect",
            "parser",
            "generator",
        }:
            selected_ids.update(_tests_in_file(tests, f"tests/dialects/test_{stem}.py"))

        if family == "dialects" and stem == "dialect" and has_unowned_point:
            selected_ids.update(_tests_in_file(tests, "tests/dialects/test_dialect.py"))

        if family == "dialects":
            selected_indices.update(dialect_fixture_refs.get(stem, []))

        if family == "parsers":
            add_known(
                "tests/test_optimizer.py::TestOptimizer::test_optimize",
                "tests/test_optimizer.py::TestOptimizer::test_qualify_tables",
                "tests/test_optimizer.py::TestOptimizer::test_quote_identifiers",
            )

        if family == "typing":
            add_known("tests/test_optimizer.py::TestOptimizer::test_optimize")

        if family == "executor":
            # Query execution can take different branches after a mutation.
            # Baseline coverage alone missed a failing TPC-DS integration test.
            selected_ids.update(_tests_in_file(tests, "tests/test_executor.py"))

        if family == "optimizer":
            add_known(
                f"tests/test_optimizer.py::TestOptimizer::test_{stem}",
                "tests/test_optimizer.py::TestOptimizer::test_optimize",
            )
            if is_control_flow_mutation(change):
                add_known(
                    "tests/test_optimizer.py::TestOptimizer::test_tpcds",
                    "tests/test_optimizer.py::TestOptimizer::test_tpch",
                    "tests/test_executor.py::TestExecutor::test_execute_subqueries",
                    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
                    "tests/test_executor.py::TestExecutor::test_execute_tpch",
                )

        if family == "expressions":
            selected_ids.update(_tests_in_file(tests, "tests/test_expressions.py"))

    selected_ids.update(
        map_test_ids[index]
        for index in selected_indices
        if 0 <= index < len(map_test_ids)
    )
    return selected_ids & known_ids


def schedule(tests: list[dict]) -> list[list[int]]:
    """Choose a low-cost partition without changing the selected tests."""

    if not tests:
        return []

    ordered = sorted(
        tests,
        key=lambda test: (round(test["seconds"] * 1000), test["number"]),
        reverse=True,
    )
    candidates: list[tuple[int, list[list[int]]]] = []
    for worker_count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers: list[list[int]] = [[] for _ in range(worker_count)]
        loads = [0] * worker_count
        for test in ordered:
            lightest = min(range(worker_count), key=loads.__getitem__)
            workers[lightest].append(test["number"])
            loads[lightest] += round(test["seconds"] * 1000)
        candidates.append((max(loads) + WORKER_COST_MS * worker_count, workers))

    # For small selections, find the optimal two-worker split exactly. A bitset
    # tracks achievable millisecond loads; checkpoints reconstruct the workers.
    if 2 <= len(ordered) <= 32:
        durations = [round(test["seconds"] * 1000) for test in ordered]
        total = sum(durations)
        if total <= 200_000:
            states = [1]
            for duration in durations:
                states.append(states[-1] | (states[-1] << duration))
            half = total // 2
            target = (states[-1] & ((1 << (half + 1)) - 1)).bit_length() - 1
            selected_load = target
            light, heavy = [], []
            for index in range(len(ordered) - 1, -1, -1):
                if (states[index] >> target) & 1:
                    heavy.append(ordered[index]["number"])
                else:
                    light.append(ordered[index]["number"])
                    target -= durations[index]
            if light and heavy:
                candidates.append((max(selected_load, total - selected_load) + 2 * WORKER_COST_MS, [light, heavy]))
    return min(candidates, key=lambda candidate: (candidate[0], len(candidate[1])))[1]


def load_impact_map(path: Path = MAP_PATH) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def main() -> None:
    payload = json.loads((DATA_DIR / "tests.json").read_text("utf-8"))
    tests: list[dict] = payload["tests"]
    tests_by_id = {test["id"]: test for test in tests}
    impact_map = load_impact_map()
    selected_total = 0

    for patch_path in sorted((DATA_DIR / "commits").glob("*.patch")):
        selected_ids = select_test_ids(
            patch_path.read_text("utf-8"), tests, impact_map
        )
        selected_tests = [tests_by_id[test_id] for test_id in selected_ids]
        selected_total += len(selected_tests)
        answer = {"commit": patch_path.stem, "workers": schedule(selected_tests)}
        print(json.dumps(answer, separators=(",", ":"), allow_nan=False))

    print(
        f"selected {selected_total} test-commit pairs for "
        f"{len(list((DATA_DIR / 'commits').glob('*.patch')))} commits",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
