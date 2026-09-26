"""Leaf runtime for the Memory facade and mechanically extracted domains.

The module accessor memoizes only the module, never its attributes. Patches and
late bindings on jarvis.memory remain authoritative for every extracted method.
"""
from __future__ import annotations

import functools
from typing import Any

DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_LEASE_SECONDS = 3_600
_MODULE: Any = None

def _memory():
    global _MODULE
    if _MODULE is None:
        from . import memory
        _MODULE = memory
    return _MODULE

def _with_recall_cache(method: Any) -> Any:
    """Activate the store's ``RecallCache`` for the duration of one recall call."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        cache = getattr(self, "_recall_cache", None)
        if cache is None:
            return method(self, *args, **kwargs)
        with cache.activate():
            return method(self, *args, **kwargs)

    return wrapper


def _with_read_snapshot(method: Any) -> Any:
    """Keep every read and validation in one coherent SQLite snapshot.

    Recall methods may be nested (hybrid recall calls semantic recall) or may
    run inside an existing transaction. Only the outermost standalone reader
    owns the deferred transaction and rolls it back after the read.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        owns_snapshot = not self.db.in_transaction
        if owns_snapshot:
            self.db.execute("BEGIN")
        try:
            return method(self, *args, **kwargs)
        finally:
            if owns_snapshot and self.db.in_transaction:
                self.db.rollback()

    return wrapper


def _with_immediate_snapshot(method: Any) -> Any:
    """Serialize a recall that also persists deterministic read telemetry."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._ensure_open()
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            if owns_transaction and self.db.in_transaction:
                self.db.rollback()
            raise
        if owns_transaction and self.db.in_transaction:
            self.db.commit()
        return result

    return wrapper
