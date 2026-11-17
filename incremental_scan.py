"""Turn native change notifications into bounded, per-library scan scopes."""

from dataclasses import dataclass
import logging
import os
from pathlib import Path, PurePosixPath
import threading
import time


MAX_TARGETS = 64
_QUIET_SECONDS = 1.25
_MAX_DELAY = 8
_WATCH_CHECK_SECONDS = 30
_LOG = logging.getLogger("sumi")


@dataclass(frozen=True, order=True)
class ScanTarget:
    root: str
    path: str
    kind: str

    def covers(self, other):
        return self == other or (self.root == other.root and self.kind == "tree"
                                 and (self.path in (".", other.path) or other.path.startswith(self.path + "/")))


def add_target(targets, target):
    if not any(item.covers(target) for item in targets):
        targets.difference_update(item for item in tuple(targets) if target.covers(item))
        targets.add(target)


def event_targets(root, data, event, classify, hidden):
    if event.event_type not in ("created", "modified", "deleted", "moved"):
        return
    if event.is_directory and event.event_type == "modified":
        return
    boundary = Path(root)
    for source, value in ((True, event.src_path), (False, event.dest_path)):
        if not value:
            continue
        candidate = Path(os.fsdecode(value))
        if not candidate.is_relative_to(boundary) or candidate.is_relative_to(data):
            continue
        relative = candidate.relative_to(boundary)
        if ".." in relative.parts or any(hidden(part) for part in relative.parts):
            continue
        kind = "tree" if event.is_directory else classify(candidate.suffix.lower())
        path = PurePosixPath(*relative.parts)
        win_delete = os.name == "nt" and source and event.event_type == "deleted" and not event.is_directory
        if win_delete:
            # ReadDirectoryChangesW sends a file-delete event even for a
            # directory that no longer exists. Rescan that exact subtree too.
            yield ScanTarget(root, path.as_posix(), "tree")
        if kind and (not win_delete or kind == "folder"):
            yield ScanTarget(root, (path.parent if kind == "folder" else path).as_posix(), kind)


class _Handler:
    def __init__(self, monitor, root):
        self.monitor = monitor
        self.root = root

    def dispatch(self, event):
        self.monitor.receive(self.root, event)


class LibraryChangeMonitor:
    def __init__(self, *, data, routes, directory, classify, hidden, submit):
        self.data = data
        self.routes = routes
        self.directory = directory
        self.classify = classify
        self.hidden = hidden
        self.submit = submit
        self.thread = None
        self._condition = threading.Condition()
        self._closed = False
        self._refresh = True
        self._routes = {}
        self._batches = {}
        self._observer = None
        self._watches = {}
        self._event_filter = None

    def start(self):
        with self._condition:
            if self.thread is None and not self._closed:
                self.thread = threading.Thread(target=self._loop, name="library-changes", daemon=True)
                self.thread.start()

    def refresh(self):
        with self._condition:
            self._refresh = True
            self._condition.notify()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self.thread is not None:
            self.thread.join(timeout=8)

    def receive(self, root, event):
        targets = tuple(event_targets(root, self.data, event, self.classify, self.hidden))
        if not targets:
            return
        now = time.monotonic()
        with self._condition:
            if self._closed:
                return
            for library_id, revision in self._routes.get(root, ()):
                self._merge(library_id, revision, targets, now)
            if any(target.kind == "tree" and target.path == "." for target in targets):
                self._refresh = True
            self._condition.notify()

    def _merge(self, library_id, revision, targets, now):
        previous = self._batches.get(library_id)
        if previous and previous[0] > revision:
            return
        _, first, _, merged = previous if previous and previous[0] == revision else (revision, now, now, set())
        if merged is not None:
            for target in targets:
                add_target(merged, target)
                if len(merged) > MAX_TARGETS:
                    merged = None  # An event burst is handled by a full scan.
                    break
        self._batches[library_id] = (revision, first, min(now + _QUIET_SECONDS, first + _MAX_DELAY), merged)

    def _drop_watch(self, root):
        watch, _ = self._watches.pop(root)
        try:
            self._observer.unschedule(watch)
        except KeyError:
            pass

    def _stop_observer(self):
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            self._watches.clear()

    def _sync(self):
        routes = self.routes()
        with self._condition:
            self._routes = routes
        if not routes:
            self._stop_observer()
            return False
        if self._observer is not None and not self._observer.is_alive():
            self._stop_observer()
        if self._observer is None:
            from watchdog.events import (DirCreatedEvent, DirDeletedEvent, DirMovedEvent,
                                         FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent)
            from watchdog.observers import Observer
            from watchdog.observers.polling import PollingObserver
            if Observer is PollingObserver:
                _LOG.warning("Native media change monitoring is unavailable; full scans remain available.")
                return False
            observer = Observer()
            observer.start()
            self._observer = observer
            self._event_filter = [DirCreatedEvent, DirDeletedEvent, DirMovedEvent,
                                  FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent]
        live = {emitter.watch for emitter in self._observer.emitters if emitter.is_alive()}
        for root in tuple(self._watches):
            if root not in routes or self._watches[root][0] not in live:
                self._drop_watch(root)
        for root, libraries in routes.items():
            try:
                info = Path(root).stat()
                identity = (info.st_dev, info.st_ino)
                if root in self._watches:
                    if self._watches[root][1] == identity:
                        continue
                    self._drop_watch(root)
                self.directory(root)
                watch = self._observer.schedule(_Handler(self, root), root, recursive=True,
                                                event_filter=self._event_filter)
                self._watches[root] = watch, identity
                # Cover changes before a new watch was ready, including a remount.
                with self._condition:
                    for library_id, revision in libraries:
                        self._merge(library_id, revision, (ScanTarget(root, ".", "tree"),), time.monotonic())
            except (OSError, ValueError, RuntimeError):
                if root in self._watches:
                    self._drop_watch(root)
                _LOG.debug("A media root could not be watched; full scans remain available.", exc_info=True)
        return True

    def _loop(self):
        next_check = float("inf")
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    now = time.monotonic()
                    refresh = self._refresh or now >= next_check
                    self._refresh = False
                    due = [(key, batch) for key, batch in self._batches.items() if batch[2] <= now]
                    if not (refresh or due):
                        wake = min((next_check, *(batch[2] for batch in self._batches.values())))
                        self._condition.wait(None if wake == float("inf") else max(0, wake - now))
                        continue
                    for key, _ in due:
                        del self._batches[key]
                if refresh:
                    try:
                        watching = self._sync()
                    except Exception as error:
                        _LOG.warning("Media change monitoring could not refresh (%s); full scans remain available.",
                                     type(error).__name__)
                        watching = True
                    next_check = time.monotonic() + _WATCH_CHECK_SECONDS if watching else float("inf")
                for library_id, (revision, _, _, targets) in due:
                    try:
                        self.submit(library_id, revision, targets)
                    except Exception as error:
                        _LOG.warning("Media change batch could not be queued (%s).", type(error).__name__)
        finally:
            self._stop_observer()
