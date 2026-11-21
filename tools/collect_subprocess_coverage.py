"""Collect line coverage for integration tests, including spawned processes.

Each pytest test runs independently so worker coverage can be attributed to the
parent test. Output contains repository-relative paths only; it is build input,
not part of the submitted runtime image.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--class-output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    result: dict[str, dict[str, list[int]]] = {}
    class_result: dict[str, list[str]] = {}
    for test in TESTS:
        with tempfile.TemporaryDirectory(prefix="rwx-child-coverage-") as directory:
            env = os.environ.copy()
            env.update({
                "COVERAGE_PROCESS_START": str(ROOT / "coverage-subprocess.ini"),
                "COVERAGE_FILE": str(Path(directory) / ".coverage"),
                "SKIP_INTEGRATION": "0",
                "PYTHONPATH": os.pathsep.join([str(ROOT / "tools/coverage_autostart"), str(repo), env.get("PYTHONPATH", "")]),
            })
            if args.class_output:
                env["RWX_CLASS_TRACE"] = directory
            run = subprocess.run([args.python, "-m", "pytest", "-q", test], cwd=repo, env=env, timeout=300)
            if run.returncode:
                raise RuntimeError(f"coverage run failed for {test}: {run.returncode}")
            files = sorted(Path(directory).glob(".coverage.*"))
            if not files:
                raise RuntimeError(f"no coverage files for {test}")
            by_file: dict[str, set[int]] = {}
            for file in files:
                data = CoverageData(basename=str(file))
                data.read()
                for name in data.measured_files():
                    try:
                        relative = Path(name).resolve().relative_to(repo).as_posix()
                    except ValueError:
                        continue
                    if relative.startswith(("sqlglot/", "tests/")):
                        by_file.setdefault(relative, set()).update(data.lines(name) or ())
            result[test] = {name: sorted(lines) for name, lines in sorted(by_file.items())}
            if args.class_output:
                class_files = list(Path(directory).glob("classes-*.json"))
                if len(class_files) < 2:
                    raise RuntimeError(f"missing child-process class traces for {test}")
                class_result[test] = sorted({name for file in class_files for name in json.loads(file.read_text())})
                print(f"{test}: {len(class_files)} class traces, {len(class_result[test])} classes", flush=True)
            print(f"{test}: {len(files)} processes, {len(by_file)} files", flush=True)
    args.output.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    if args.class_output:
        args.class_output.write_text(json.dumps(class_result, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
