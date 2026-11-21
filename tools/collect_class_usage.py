"""Pytest plugin that records which tests instantiate sqlglot runtime classes.

Load this only while building ``impact_map.json.gz``::

    CLASS_USAGE_OUTPUT=class-usage.json pytest -q -p tools.collect_class_usage

The submission runtime does not import or need this module.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path


_current_test: str | None = None
_usage: dict[str, set[str]] = defaultdict(set)


def _record(cls: type) -> None:
    if _current_test is not None:
        test_id = _current_test.split("[", 1)[0]
        _usage[f"{cls.__module__}.{cls.__qualname__}"].add(test_id)


def _instrument(base: type) -> None:
    original_new = base.__new__

    def tracking_new(cls: type, *args: object, **kwargs: object) -> object:
        _record(cls)
        if original_new is object.__new__:
            return object.__new__(cls)
        return original_new(cls, *args, **kwargs)

    base.__new__ = tracking_new


def pytest_configure(config: object) -> None:
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for base in (Expr, Dialect, Generator, Parser, Tokenizer):
        _instrument(base)


def pytest_runtest_protocol(item: object, nextitem: object) -> None:
    global _current_test
    _current_test = item.nodeid


def pytest_runtest_logfinish(nodeid: str, location: object) -> None:
    global _current_test
    _current_test = None


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    output = Path(os.environ["CLASS_USAGE_OUTPUT"])
    payload = {key: sorted(value) for key, value in sorted(_usage.items())}
    output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
