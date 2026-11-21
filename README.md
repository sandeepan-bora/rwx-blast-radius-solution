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

The selector combines four reusable signals:

1. **Per-function coverage scopes.** `impact_map.json.gz` records which tests
   executed each function in the unmodified pinned repository.
2. **Exact fixture routing.** An optimizer fixture such as
   `pushdown_predicates.sql` routes to its matching optimizer test instead of the
   entire optimizer file.
3. **Family fallbacks.** Dialect/parser/generator changes include their direct
   dialect tests; shared or import-time behavior gets small conservative sentinels.
4. **Patch risk.** Comment-only files select nothing. Optimizer control-flow
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
source = sqlglot
dynamic_context = test_function
```

Collect coverage from the unmodified checkout:

```sh
SKIP_INTEGRATION=0 coverage run --rcfile=/path/to/coverage.ini -m pytest -q
```

Then build the map from this repository:

```sh
python tools/build_impact_map.py \
  --repo /path/to/sqlglot \
  --coverage-data /path/to/sqlglot/.coverage \
  --tests-json sample/tests.json \
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
- Full pinned suite used to build the map: **1,620 passed, 1 skipped in 95.90 s**.
- Optimized sample, repeated three times: **139.731 simulated seconds, 0 misses**
  each time; all three JSONL outputs were byte-identical.
- Local selector CPU proxy: median **0.03 s user + 0.00 s system**, implying
  about **139.8 s** after tenth-second score rounding on this machine. Server CPU
  accounting and hardware can differ.
- Unit/integration tests: **5 passed**.
- Submission directory size during verification: **684 KB**, below the 20 MB
  repository limit.
- Docker was not available on the verification host, so `make sample` itself was
  not executed there. The same selector output was checked directly with the
  supplied official `score.py`.
- Contest/leaderboard score: **not measured**. Do not treat a local sample number
  as a guaranteed hidden-set or leaderboard result.

## Submission notes

- Runtime dependencies: Python standard library only.
- Runtime network: none.
- Runtime writes: none (compatible with a read-only filesystem and 64 MB `/tmp`).
- Stdout: strict JSONL answers only; diagnostics go to stderr.
- The Docker build is fast because the coverage map is committed rather than
  regenerated during every submission build.
