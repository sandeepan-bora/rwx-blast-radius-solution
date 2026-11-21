#!/usr/bin/env python3
"""Build a compact, deterministic impact map from pinned-repository coverage.

Expected coverage configuration:

    [run]
    source = sqlglot
    dynamic_context = test_function

Run the unmodified pinned suite with SKIP_INTEGRATION=0 before this script.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import re
from pathlib import Path

from coverage import CoverageData


PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def test_context(test_id: str) -> str:
    path, *parts = test_id.split("::")
    return f"{path[:-3].replace('/', '.')}.{'.'.join(parts)}"


def fixture_test_indices(path: str, test_ids: list[str]) -> list[int]:
    parts = Path(path).parts
    group = parts[2].lower() if len(parts) > 2 else ""
    stem = Path(path).stem.lower()
    candidates = {stem, stem.rstrip("s")}
    candidates.update(
        {
            "annotate_funcs" if stem == "annotate_functions" else stem,
            "optimize" if stem == "optimizer" else stem,
        }
    )
    if stem.endswith("ies"):
        candidates.add(f"{stem[:-3]}y")

    selected: list[int] = []
    for index, test_id in enumerate(test_ids):
        method = test_id.rsplit("::", 1)[-1].removeprefix("test_").lower()
        if group == "optimizer":
            if test_id.startswith("tests/test_optimizer.py::") and any(
                method == candidate
                or method.startswith(f"{candidate}__")
                or candidate.startswith(f"{method}_")
                for candidate in candidates
            ):
                selected.append(index)
        elif group and test_id.startswith(f"tests/dialects/test_{group}.py::"):
            selected.append(index)

    # These fixture owners are visible in the pinned suite but do not follow
    # the optimizer filename convention above (root fixtures and TPC data).
    explicit_ids: set[str] = set()
    if path == "tests/fixtures/identity.sql":
        explicit_ids.update(
            {
                "tests/test_serde.py::TestSerde::test_serde",
                "tests/test_transpile.py::TestTranspile::test_identity",
            }
        )
    elif path == "tests/fixtures/partial.sql":
        explicit_ids.add("tests/test_transpile.py::TestTranspile::test_partial")
    elif path == "tests/fixtures/pretty.sql":
        explicit_ids.add("tests/test_transpile.py::TestTranspile::test_pretty")
    elif path.endswith("/jsonpath/cts.json"):
        explicit_ids.add("tests/test_jsonpath.py::TestJsonpath::test_cts_file")
    elif "/tpc-ds/" in path:
        explicit_ids.update(
            {
                "tests/test_optimizer.py::TestOptimizer::test_tpcds",
                "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            }
        )
    elif "/tpc-h/" in path:
        explicit_ids.update(
            {
                "tests/test_optimizer.py::TestOptimizer::test_tpch",
                "tests/test_executor.py::TestExecutor::test_execute_tpch",
                "tests/test_executor.py::TestExecutor::test_optimized_tpch",
            }
        )

    selected.extend(
        index for index, test_id in enumerate(test_ids) if test_id in explicit_ids
    )
    return sorted(set(selected))


def _class_scopes(
    tree: ast.AST,
    module: str,
    usage: dict[str, list[str]],
    index_by_id: dict[str, int],
    contexts_by_line: dict[int, set[int]],
) -> list[list]:
    scopes: list[list] = []

    def visit(body: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            qualname = ".".join((*parents, node.name))
            selected = {
                index_by_id[test_id]
                for test_id in usage.get(f"{module}.{qualname}", ())
                if test_id in index_by_id
            }
            # Generic classes are not part of the runtime usage tracer.  Tests
            # that execute any method of such a class are still a strong owner
            # signal for constants and registries declared in its body.
            for line in range(node.lineno, node.end_lineno + 1):
                selected.update(contexts_by_line.get(line, ()))
            if selected:
                scopes.append([node.lineno, node.end_lineno, sorted(selected)])
            visit(node.body, (*parents, node.name))

    visit(getattr(tree, "body", []))
    return sorted(scopes, key=lambda scope: (scope[0], scope[1]))


def build_map(
    repo: Path,
    coverage_path: Path,
    tests_path: Path,
    class_usage_path: Path | None = None,
) -> dict:
    tests = json.loads(tests_path.read_text("utf-8"))["tests"]
    test_ids = [test["id"] for test in tests]
    index_by_id = {test_id: index for index, test_id in enumerate(test_ids)}
    index_by_context = {
        test_context(test_id): index for index, test_id in enumerate(test_ids)
    }
    class_usage = (
        json.loads(class_usage_path.read_text("utf-8")) if class_usage_path else {}
    )

    data = CoverageData(basename=str(coverage_path.resolve()))
    data.read()
    files: dict[str, dict] = {}
    repo = repo.resolve()

    for measured_path in sorted(data.measured_files()):
        source_path = Path(measured_path).resolve()
        try:
            relative = source_path.relative_to(repo).as_posix()
        except ValueError:
            continue
        if not relative.startswith(("sqlglot/", "tests/")):
            continue

        contexts_by_line: dict[int, set[int]] = {}
        for line, contexts in data.contexts_by_lineno(measured_path).items():
            contexts_by_line[line] = {
                index_by_context[context]
                for context in contexts
                if context in index_by_context
            }

        all_indices = sorted(set().union(*contexts_by_line.values())) if contexts_by_line else []
        scopes: list[list] = []
        classes: list[list] = []
        try:
            tree = ast.parse(source_path.read_text("utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                selected: set[int] = set()
                for line in range(node.lineno, node.end_lineno + 1):
                    selected.update(contexts_by_line.get(line, ()))
                if selected:
                    scopes.append([node.lineno, node.end_lineno, sorted(selected)])
            module = relative[:-3].replace("/", ".")
            classes = _class_scopes(
                tree,
                module,
                class_usage,
                index_by_id,
                contexts_by_line,
            )
        scopes.sort(key=lambda scope: (scope[0], scope[1]))
        files[relative] = {"all": all_indices, "scopes": scopes, "classes": classes}

    fixtures: dict[str, list[int]] = {}
    dialect_fixture_refs: dict[str, set[int]] = {}
    for fixture_path in sorted((repo / "tests" / "fixtures").rglob("*")):
        if not fixture_path.is_file():
            continue
        relative = fixture_path.relative_to(repo).as_posix()
        owners = fixture_test_indices(relative, test_ids)
        fixtures[relative] = owners
        try:
            text = fixture_path.read_text("utf-8").lower()
        except UnicodeDecodeError:
            continue
        for dialect in re.findall(r"dialect\s*:\s*([a-z0-9_]+)", text):
            dialect_fixture_refs.setdefault(dialect, set()).update(owners)

    return {
        "schema": 2,
        "pinned_commit": PINNED_COMMIT,
        "test_ids": test_ids,
        "files": files,
        "fixtures": fixtures,
        "dialect_fixture_refs": {
            dialect: sorted(indices)
            for dialect, indices in sorted(dialect_fixture_refs.items())
        },
    }


def write_deterministic_gzip(payload: dict, output: Path) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--coverage-data", type=Path, required=True)
    parser.add_argument("--tests-json", type=Path, required=True)
    parser.add_argument("--class-usage", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    write_deterministic_gzip(
        build_map(
            options.repo,
            options.coverage_data,
            options.tests_json,
            options.class_usage,
        ),
        options.output,
    )


if __name__ == "__main__":
    main()
