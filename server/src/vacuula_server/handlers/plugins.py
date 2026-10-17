from __future__ import annotations
import logging

from vacuula_server import plugins

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from vacuula_server.handlers.router import RouterHandler

logger = logging.getLogger(__name__)


def get_plugins(req: RouterHandler) -> None:
    """GET /api/plugins - every plugin found, with status and config"""
    req.send_ok(plugins.list_plugins())


def post_plugins_reload(req: RouterHandler) -> None:
    """POST /api/plugins/reload - rescan plugin folders and re-import plugin code"""
    plugins.load_all()
    req.send_ok(plugins.list_plugins())


def post_plugin(req: RouterHandler, plugin_id: str) -> None:
    """POST /api/plugins/<id> - {"enabled"?: bool, "config"?: {...}}"""
    data = req.read_json()
    if not isinstance(data, dict):
        if data is not None: req.send_bad_request("Expected a JSON object")
        return

    enabled = data.get("enabled")
    config = data.get("config")
    if enabled is not None and not isinstance(enabled, bool):
        return req.send_bad_request("enabled must be true or false")
    if config is not None and not isinstance(config, dict):
        return req.send_bad_request("config must be an object")

    try:
        req.send_ok(plugins.update_plugin(plugin_id, enabled, config))
    except KeyError:
        req.send_not_found("Plugin not found")
    except ValueError as e:
        req.send_bad_request(str(e))


def plugin_route(req: RouterHandler, plugin_id: str, path: str) -> None:
    """ANY /api/plugins/<id>/<path> - routes registered by a running plugin"""
    handler, params = plugins.find_plugin_route(plugin_id, req.command, path)
    if handler is None:
        return req.send_not_found()
    try:
        handler(req, *params)
    except Exception:
        logger.exception("Plugin %s route %s %s failed", plugin_id, req.command, path)
        req.send_internal_error("Plugin error")
