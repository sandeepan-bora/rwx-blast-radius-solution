# RWX Blast Radius — coverage-guided static selector

This repository is a contest-compliant submission for the sqlglot Blast Radius
track. It analyzes each independent patch, chooses a test subset, and schedules
that subset on up to eight workers. It does **not** run sqlglot during scoring and
does not contain a patch-to-answer lookup table.

## The scoring idea, in plain language

For each commit, the selector is making one prediction: “these are the tests this
patch could break.” The workers only run that chosen subset in parallel. They do
not reproduce future commits, learn future cases, or invent tests. If even one
actually failing test was omitted, that commit receives a 300-second penalty.

For a non-empty selection, the commit cost is:

```text
time on the busiest worker + 2 seconds × number of non-empty workers
```

Running nothing costs zero, so harmless changes are valuable to identify. More
workers reduce the busiest load but add overhead. `selector.py` tries every worker
count from 1 through 8 using the recorded millisecond durations and keeps the
lowest-cost schedule.

Your original understanding was close on the first half: the system scans code
changes and schedules relevant tests. The correction is that the workers are not
trying to replicate test cases or solve unknown future cases. Every future commit
arrives as a new patch; the same static selector analyzes that patch and chooses
its tests independently.

## Design

The selector combines seven reusable signals:

1. **Per-function coverage scopes.** `impact_map.json.gz` records which tests
   executed each function in the unmodified pinned repository.
2. **Runtime class ownership.** Build-time tracing records which tests
   instantiate each expression, dialect, parser, generator, and tokenizer
   class. This covers class constants that execute before per-test coverage
   begins.
3. **Exact fixture routing.** An optimizer fixture such as
   `pushdown_predicates.sql` routes to its matching optimizer test instead of the
   entire optimizer file. Root, JSONPath, and TPC fixtures have explicit owners
   derived from the pinned suite.
4. **Shared test-helper coverage.** Changes to `tests/helpers.py` route to the
   tests that execute the changed helper instead of selecting nothing.
5. **Cross-module symbol ownership.** A static import/use map connects module
   constants and imported classes to the tests covering their consumers.
6. **Family fallbacks.** Dialect/parser/generator changes include their direct
   dialect tests; shared or import-time behavior gets small conservative sentinels.
7. **Patch risk.** Comment-only files select nothing. Optimizer control-flow
   mutations add the slow integration sentinels because their blast radius is
   substantially wider.

The prebuilt map was derived only from sqlglot commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa` with dynamic per-test coverage and
`SKIP_INTEGRATION=0`. The sample `answers.json` is used only by local evaluation,
not by the selector or map builder.

## Reproduce the map

Use Python 3.13 and the dependency versions in the supplied contest
`requirements.lock`, plus `coverage`. In a checkout of the pinned sqlglot commit:

Put this in a coverage configuration before running the suite (the file is
included here as `coverage.ini`):

```ini
[run]
source = sqlglot,tests
dynamic_context = test_function
```

Collect coverage from the unmodified checkout (including shared test helpers):

```sh
SKIP_INTEGRATION=0 coverage run --rcfile=/path/to/coverage.ini -m pytest -q
```

Collect runtime class ownership in a separate clean test run:

```sh
CLASS_USAGE_OUTPUT=/tmp/class-usage.json \
  PYTHONPATH=/path/to/this-repository \
  SKIP_INTEGRATION=0 \
  python -m pytest -q -p tools.collect_class_usage
```

Then build the map from this repository:

```sh
python tools/build_impact_map.py \
  --repo /path/to/sqlglot \
  --coverage-data /path/to/sqlglot/.coverage \
  --tests-json sample/tests.json \
  --class-usage /tmp/class-usage.json \
  --output impact_map.json.gz
```

The gzip output is deterministic (`mtime=0`) and safe to commit as a plain file.

## Test locally

```sh
python3 -m unittest discover -s tests -v
make sample
```

`make sample` requires Docker and reproduces the scorer's read-only, offline,
non-root runtime. The unit/integration test can run without Docker. The exact
sample score and local CPU measurement are recorded below after verification.

## Verification status

- Pinned test collection: 1,620 scored tests plus the one test listed as skipped.
- Unchanged starter baseline: **1,210.470 simulated seconds, 0 misses**.
- Pinned coverage run used to build the map: **1,620 passed, 1 skipped in
  96.21 s**. The separate class-ownership run also passed all 1,620 tests with
  one skip.
- Original v1 sample: **139.731 simulated seconds, 0 misses**. Its first practice
  submission (`fa44e83`) exposed hidden-set blind spots and scored **1620.7 s**:
  120.371 simulated test seconds plus five 300-second miss penalties, with
  about 0.33 CPU seconds. This result was deterministic across all three runs.
- Revised v2 sample: **150.533 simulated seconds, 0 misses**; **150.573 total**
  when supplied a conservative 0.04-second local CPU measurement. The added
  ~10.8 simulated seconds are a deliberate recall premium for class constants,
  previously unowned fixtures, and shared test helpers.
- Revised hidden/practice score: **not measured yet**. Any sub-160 estimate is a
  projection, not a verified leaderboard result.
- Unit/integration tests: **9 passed**.
- Submission directory size during verification: **1.2 MB**, below the 20 MB
  repository limit.
- Docker was not available on the verification host, so `make sample` itself was
  not executed there. The same selector output was checked directly with the
  supplied official `score.py`.

## Submission notes

- Runtime dependencies: Python standard library only.
- Runtime network: none.
- Runtime writes: none (compatible with a read-only filesystem and 64 MB `/tmp`).
- Stdout: strict JSONL answers only; diagnostics go to stderr.
- The Docker build is fast because the coverage map is committed rather than
  regenerated during every submission build.
