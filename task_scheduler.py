"""Bound CPU-heavy image work across the service without holding database locks."""

from collections import deque
from contextlib import closing, contextmanager
from dataclasses import dataclass
import threading


MAX_TASK_CONCURRENCY = 8
_POLL_SECONDS = .1
_PRIORITY_BURST = 8


@dataclass(eq=False)
class _Waiter:
    thread_id: int
    resource: object
    priority: bool


class SharedTaskScheduler:
    """A service-wide lease pool with FIFO queues and serial task families.

    Leases should cover one image, rather than a whole catalogue job, so other
    jobs can make progress. A named resource serializes a task family even when
    the administrator allows several families to run at once. Nested leases on
    the same thread and named resource share their outer lease.
    """

    def __init__(self, library, error_type):
        self.library = library
        self._error = error_type
        self._condition = threading.Condition()
        self._configure_lock = threading.Lock()
        self._closed = False
        self._active = 0
        self._resources = {}
        self._regular = deque()
        self._priority = deque()
        self._priority_streak = 0
        with closing(library.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS performance_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                task_concurrency INTEGER NOT NULL DEFAULT 1
                    CHECK(task_concurrency BETWEEN 1 AND 8)
            )""")
            connection.execute("INSERT OR IGNORE INTO performance_settings(id) VALUES(1)")
            row = connection.execute(
                "SELECT task_concurrency FROM performance_settings WHERE id=1"
            ).fetchone()
            self._concurrency = int(row[0])

    @property
    def concurrency(self):
        with self._condition:
            return self._concurrency

    def snapshot(self):
        return {"taskConcurrency": self.concurrency,
                "maxTaskConcurrency": MAX_TASK_CONCURRENCY}

    def snapshot_debug(self):
        """Read-only process state for lifecycle and concurrency checks."""
        with self._condition:
            return {"active": self._active,
                    "queued": len(self._regular) + len(self._priority),
                    "priorityQueued": len(self._priority),
                    "closed": self._closed}

    def waiting_count(self, *, priority=False, resource=None):
        """Count registered waiters, never callers still checking cancellation."""
        with self._condition:
            queue = self._priority if priority else self._regular
            return sum(resource is None or waiter.resource == resource for waiter in queue)

    def configure(self, body):
        # Serialize commits and their in-memory publication so a slower caller
        # cannot publish an older setting after a newer one has committed.
        with self._configure_lock:
            with closing(self.library.connect()) as connection, connection:
                self.library.require_admin(connection)
                if (not isinstance(body, dict) or set(body) != {"taskConcurrency"}
                        or type(body["taskConcurrency"]) is not int
                        or not 1 <= body["taskConcurrency"] <= MAX_TASK_CONCURRENCY):
                    raise self._error("同时任务数需要是 1 到 8 之间的整数。")
                value = body["taskConcurrency"]
                connection.execute("UPDATE performance_settings SET task_concurrency=? WHERE id=1", (value,))
            with self._condition:
                self._concurrency = value
                self._condition.notify_all()
        return self.snapshot()

    def wake(self):
        with self._condition:
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _eligible(self, queue):
        # An occupied family does not prevent another family from using a free
        # slot. Within each family and priority class arrival order is retained.
        return next((item for item in queue
                     if item.resource is None or item.resource not in self._resources), None)

    def _next(self):
        priority = self._eligible(self._priority)
        regular = self._eligible(self._regular)
        if regular is not None and (priority is None or self._priority_streak >= _PRIORITY_BURST):
            return regular
        return priority if priority is not None else regular

    def _remove(self, waiter):
        queue = self._priority if waiter.priority else self._regular
        if waiter in queue:
            queue.remove(waiter)
            self._condition.notify_all()

    @contextmanager
    def slot(self, *, cancel=None, priority=False, resource=None):
        """Yield whether a lease was acquired; cancellation never consumes one.

        ``cancel`` is a no-argument callback. It may access SQLite or other
        manager locks: it is deliberately evaluated outside our condition.
        Lowering the limit only affects new leases; running images finish.
        """
        thread_id = threading.get_ident()
        waiter = _Waiter(thread_id, resource, bool(priority))
        acquired = False
        nested = False
        queued = False
        try:
            while True:
                cancelled = bool(cancel()) if cancel is not None else False
                with self._condition:
                    if self._closed or cancelled:
                        if queued:
                            self._remove(waiter)
                            queued = False
                        break
                    owner = self._resources.get(resource) if resource is not None else None
                    if owner is not None and owner[0] == thread_id:
                        owner[1] += 1
                        nested = acquired = True
                        break
                    if not queued:
                        (self._priority if waiter.priority else self._regular).append(waiter)
                        queued = True
                    if self._active < self._concurrency and self._next() is waiter:
                        self._remove(waiter)
                        queued = False
                        self._active += 1
                        if resource is not None:
                            self._resources[resource] = [thread_id, 1]
                        # Count waiting background work even when this newly
                        # granted lease temporarily occupies its task family.
                        # Eligibility is reconsidered after the lease releases.
                        self._priority_streak = (self._priority_streak + 1
                                                 if waiter.priority and self._regular else 0)
                        acquired = True
                        self._condition.notify_all()
                        break
                    self._condition.wait(_POLL_SECONDS)
            yield acquired
        finally:
            with self._condition:
                if queued:
                    self._remove(waiter)
                if acquired:
                    if resource is not None:
                        owner = self._resources[resource]
                        owner[1] -= 1
                        if not owner[1]:
                            del self._resources[resource]
                    if not nested:
                        self._active -= 1
                    self._condition.notify_all()
