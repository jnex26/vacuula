"""In-process event bus.

Robot connections emit events here; everything that reacts to them (the web app
SSE streams, plugins) subscribes. Each subscriber gets its own bounded queue and
worker thread, so a slow or broken subscriber can never stall a robot's read loop
or delay other subscribers. Events are delivered to a subscriber in emit order.

Events:
  robot.connected     {"serial", "type"}
  robot.disconnected  {"serial", "type"}
  robot.renamed       {"serial", "name"}
  robot.event         {"serial", "type", "data", "ts"}  (+ "event" for gen3)
"""
from __future__ import annotations
import fnmatch
import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from vacuula_server.state import put_drop_oldest

Handler = Callable[[str, dict[str, Any]], None]

SUBSCRIBER_QUEUE_MAX = 1000  # events buffered per subscriber before the oldest is dropped

logger = logging.getLogger(__name__)


class Subscription:
    def __init__(self, patterns: tuple[str, ...], handler: Handler, name: str):
        self.patterns = patterns
        self.handler = handler
        self.name = name
        self.queue: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"bus:{name}", daemon=True)

    def matches(self, event: str) -> bool:
        return any(fnmatch.fnmatchcase(event, p) for p in self.patterns)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                event, data = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handler(event, data)
            except Exception:
                logger.exception("bus subscriber %s failed on %s", self.name, event)

    def unsubscribe(self) -> None:
        with _lock:
            if self in _subscriptions:
                _subscriptions.remove(self)
        self.stop_event.set()


_subscriptions: list[Subscription] = []
_lock = threading.Lock()


def subscribe(patterns: str | list[str], handler: Handler, name: str | None = None) -> Subscription:
    """Call handler(event, data) for every event matching a glob pattern, e.g. "robot.*"."""
    if isinstance(patterns, str):
        patterns = [patterns]
    sub = Subscription(tuple(patterns), handler, name or getattr(handler, "__qualname__", "subscriber"))
    with _lock:
        _subscriptions.append(sub)
    sub.thread.start()
    return sub


def emit(event: str, data: dict[str, Any]) -> None:
    """Queue an event for every matching subscriber. Never blocks, never raises."""
    with _lock:
        subs = [s for s in _subscriptions if s.matches(event)]
    for sub in subs:
        put_drop_oldest(sub.queue, (event, data))
