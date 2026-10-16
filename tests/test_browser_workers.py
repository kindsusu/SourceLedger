"""Teardown grace, deadline and thread ownership regressions."""
from queue import Queue
from threading import Event, Thread, get_ident
import time

from su_crawler.browser_workers import stop_and_join_workers


def test_normal_cleanup_can_exceed_old_two_second_limit():
    tasks, stop = Queue(), Event()
    owners = []

    def worker():
        tasks.get()
        time.sleep(2.1)
        owners.append(get_ident())

    thread = Thread(target=worker)
    thread.start()
    assert stop_and_join_workers([thread], tasks, stop)
    assert owners == [thread.ident]
    assert stop.is_set()


def test_shutdown_deadline_is_shared_and_live_worker_is_not_accepted(monkeypatch):
    from su_crawler import browser_workers

    clock = [0.0]
    waits = []
    monkeypatch.setattr(browser_workers.time, 'monotonic', lambda: clock[0])

    class SlowWorker:
        def join(self, timeout):
            waits.append(timeout)
            clock[0] += timeout

        def is_alive(self):
            return True

    tasks, stop = Queue(), Event()
    assert not stop_and_join_workers([SlowWorker(), SlowWorker()], tasks, stop, timeout=3)
    assert waits == [3, 0]
    assert stop.is_set() and tasks.qsize() == 2


def test_idle_workers_receive_stop_signal():
    tasks, stop = Queue(), Event()
    workers = [Thread(target=tasks.get) for _ in range(2)]
    for worker in workers:
        worker.start()
    assert stop_and_join_workers(workers, tasks, stop)
