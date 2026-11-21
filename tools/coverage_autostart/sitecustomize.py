"""Enable coverage in spawned test workers when explicitly configured."""

import coverage
import os

coverage.process_startup()

if os.environ.get("RWX_CLASS_TRACE"):
    import atexit
    import json
    from pathlib import Path

    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    used_classes = set()

    def instrument(base):
        original_new = base.__new__

        def tracking_new(cls, *args, **kwargs):
            used_classes.add(f"{cls.__module__}.{cls.__qualname__}")
            if original_new is object.__new__:
                return object.__new__(cls)
            return original_new(cls, *args, **kwargs)

        base.__new__ = tracking_new

    for base_class in (Expr, Dialect, Generator, Parser, Tokenizer):
        instrument(base_class)

    def save_classes():
        path = Path(os.environ["RWX_CLASS_TRACE"]) / f"classes-{os.getpid()}.json"
        path.write_text(json.dumps(sorted(used_classes)), encoding="utf-8")

    atexit.register(save_classes)
