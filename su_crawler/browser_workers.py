"""Bounded shutdown of browser workers on their owning threads."""
from __future__ import annotations

import time
from queue import Queue
from threading import Event, Thread


def stop_and_join_workers(workers: list[Thread], tasks: Queue, stop: Event,
                          *, timeout: float = 15.0) -> bool:
    """Allow normal Chromium teardown without accepting an orphaned worker.

    Use one shared grace period, rather than multiplying it by worker count.
    Callers must fail closed when False is returned; the helper never closes
    Playwright resources from another thread or kills unrelated browsers.
    """
    stop.set()
    for _ in workers:
        tasks.put(None)
    deadline = time.monotonic() + max(0.0, timeout)
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return all(not worker.is_alive() for worker in workers)
