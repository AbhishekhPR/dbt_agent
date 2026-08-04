"""A small bounded store pool for the public API.

psycopg connections are not safe to share across concurrent requests, so each
in-flight request checks out its own store. A bounded pool keeps the server
within the application role's connection limit while still allowing genuine
concurrency (which the idempotency and race tests depend on).

Deliberately hand-rolled rather than pulling in psycopg_pool: it keeps the
hash-locked dependency set unchanged for a handful of lines of queue logic.
"""
from __future__ import annotations

import queue
import threading
from contextlib import contextmanager


class StorePool:
    def __init__(self, factory, *, size: int = 5):
        if size < 1:
            raise ValueError("Store pool size must be positive.")
        self._factory = factory
        self._size = size
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    @contextmanager
    def acquire(self, timeout: float = 30.0):
        store = self._checkout(timeout)
        broken = False
        try:
            yield store
        except Exception:
            # A failed statement can leave the session in an aborted
            # transaction; discard rather than hand it to the next request.
            broken = True
            raise
        finally:
            self._checkin(store, discard=broken)

    def _checkout(self, timeout: float):
        with self._lock:
            if self._closed:
                raise RuntimeError("Store pool is closed.")
            may_create = self._created < self._size
            if may_create:
                self._created += 1
        if may_create:
            try:
                return self._factory()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            return self._idle.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Timed out waiting for a database connection.") from None

    def _checkin(self, store, *, discard: bool):
        if discard or self._closed:
            with self._lock:
                self._created -= 1
            try:
                store.close()
            except Exception:
                pass
            return
        self._idle.put(store)

    def close(self):
        with self._lock:
            self._closed = True
        while True:
            try:
                store = self._idle.get_nowait()
            except queue.Empty:
                return
            try:
                store.close()
            except Exception:
                pass
