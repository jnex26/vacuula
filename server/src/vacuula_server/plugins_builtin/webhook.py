"""POST robot events as JSON to a URL, e.g. a Home Assistant or Node-RED webhook."""
from __future__ import annotations
import json
import urllib.error
import urllib.request

NAME = "Webhook"
DESCRIPTION = "POST robot events as JSON to a URL, e.g. a Home Assistant or Node-RED webhook."
VERSION = "1.0.0"
CONFIG = [
    {"key": "url", "label": "URL", "type": "text", "default": ""},
    {"key": "events", "label": "Events (comma separated, * wildcard)", "type": "text", "default": "robot.*"},
    {"key": "robots", "label": "Robot serials (comma separated, empty for all)", "type": "text", "default": ""},
    {"key": "timeout", "label": "Timeout (seconds)", "type": "number", "default": 10},
]


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def setup(ctx):
    url = ctx.config["url"].strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    robots = set(_split(ctx.config["robots"]))
    timeout = float(ctx.config["timeout"])

    def post(event: str, data: dict) -> None:
        if robots and data.get("serial") not in robots:
            return
        body = json.dumps({"event": event, **data}).encode()
        request = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
        except (urllib.error.URLError, OSError) as e:
            ctx.logger.warning("webhook %s for %s failed: %s", event, data.get("serial"), e)

    ctx.subscribe(_split(ctx.config["events"]) or ["robot.*"], post)
