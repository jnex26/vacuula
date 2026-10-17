"""Keep the latest robot events in memory and serve them at /api/plugins/event_log/events."""
from __future__ import annotations
import collections

NAME = "Event log"
DESCRIPTION = "Keeps the latest robot events in memory, view them at /api/plugins/event_log/events. Handy when writing plugins."
VERSION = "1.0.0"
CONFIG = [
    {"key": "size", "label": "Events to keep", "type": "number", "default": 100},
]


def setup(ctx):
    events: collections.deque = collections.deque(maxlen=max(1, int(ctx.config["size"])))

    ctx.subscribe("*", lambda event, data: events.append({"event": event, **data}))
    ctx.add_route("GET", "events", lambda req: req.send_ok(list(events)))

    def clear(req):
        events.clear()
        req.send_ok()

    ctx.add_route("DELETE", "events", clear)
