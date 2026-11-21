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
workers reduce the busiest load but add overhead. `selector.py` tries worker
counts from 1 through 8, and also checks the exact optimal two-worker split for
small selections. It fills unused worker capacity with additional short tests,
without increasing the recorded test time or the number of workers. It keeps
the lowest-cost schedule without dropping tests; its own CPU time is charged
separately.

Your original understanding was close on the first half: the system scans code
changes and schedules relevant tests. The correction is that the workers are not
trying to replicate test cases or solve unknown future cases. Every future commit
arrives as a new patch; the same static selector analyzes that patch and chooses
its tests independently.

## Design

The selector combines seven reusable signals:

1. **Per-function coverage scopes.** `impact_map.json.gz` records which tests
   executed each function in the unmodified pinned repository, including the
   spawned workers of the two TPC integration tests.
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
7. **Patch risk and fallbacks.** Python comments outside multiline strings select
   nothing, but fixture metadata is treated as input. Unowned code uses file-level
   coverage, unknown relevant inputs use the full suite, and executor changes
   include executor integration tests.

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

Collect the two multiprocessing integration tests independently, attributing
each spawned process to the test that started it:

```sh
python tools/collect_subprocess_coverage.py \
  --repo /path/to/sqlglot --output /tmp/subprocess-coverage.json \
  --python /path/to/python
```

Then build the map from this repository:

```sh
python tools/build_impact_map.py \
  --repo /path/to/sqlglot \
  --coverage-data /path/to/sqlglot/.coverage \
  --tests-json sample/tests.json \
  --class-usage /tmp/class-usage.json \
  --subprocess-coverage /tmp/subprocess-coverage.json \
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

`selector_fast.py` and `Dockerfile.fast` now invoke the same reliable selector.
The previous shortcut scored **150.533 simulated seconds** on the public sample
but missed TPC-DS in two mutations; it was removed. Both entry points now choose
the same tests. Contest submission builds the root `Dockerfile`.

To check recall with independent mutations of the pinned checkout:

```sh
python tools/mutation_check.py --repo /path/to/sqlglot --python /path/to/python
```

The interpreter needs the contest test dependencies. Each mutation runs the full
suite in a temporary copy and compares actual failing tests with selected tests.
Zero-failure mutations are reported separately; misses, unknown failures,
unexpected skips, collection errors, and timeouts cause a nonzero exit.

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
- Revised v2 practice score: **480.2 s**, including one missed commit (300 s).
- Prior v3 public sample: **172.654 simulated seconds, zero misses**.
- Current public sample: **225.506 simulated seconds, zero misses**. The
  additional spawned-process coverage catches dependencies the original map
  could not observe. This is not a hidden or final score, and the hidden missed
  commit is not identified, so zero hidden misses cannot be guaranteed.
- Idle-capacity filling and further recall fixes increased selected test/commit
  pairs from 1,222 to **10,372**. Idle filling alone did not increase any
  commit's simulated cost. This
  is a recall improvement, not proof that any hidden miss was repaired; CPU
  overhead remains separately chargeable. Base-dialect changes also include the
  optimizer tests after a mutation exposed two previously missed failures.
- For the earlier 212.694-second sample, the chosen schedules were optimal for
  their selected tests; reaching 150 requires different selections, not merely
  changing their worker distribution.
- Five full-suite mutations produced 32 failing test cases in total; all were
  selected. A separate class-schema mutation produced no failing tests and
  therefore provides no evidence of recall. These mutations do not establish
  performance on hidden cases.
- The two mutations that defeated the retired fast shortcut now select every
  failing test: 50 failures for the table mutation and 21 for the range mutation,
  with no unexpected skips or unrecognized failures.
- A dialect-normalization mutation produced 58 failures; all are selected after
  the optimizer fallback was added. A separate optimizer-normalization mutation
  timed out during local evaluation and remains unverified.
- Parser fixtures now include optimizer tests that explicitly name the affected
  dialect; a BigQuery mutation with five failures is covered. Schema changes
  include optimizer tests after a mutation exposed two missing failures.
- Shared helper changes include optimizer tests, while tokenizer core changes
  select the full suite. Full-suite mutation checks found 2 helper failures and
  19 tokenizer failures respectively; all were selected. These address two
  independently reported blind spots but do not identify the hidden miss.
- Unit/integration tests: **27 passed**.
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
