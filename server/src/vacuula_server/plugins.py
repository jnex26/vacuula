"""Plugin system.

A plugin is a Python module that defines `setup(ctx)` and optionally:

    NAME = "My plugin"
    DESCRIPTION = "What it does"
    VERSION = "1.0.0"
    CONFIG = [  # rendered as a form on the Plugins page
        {"key": "url", "label": "URL", "type": "text", "default": ""},
        {"key": "verbose", "label": "Verbose", "type": "switch", "default": False},
        {"key": "retries", "label": "Retries", "type": "number", "default": 3},
        {"key": "mode", "label": "Mode", "type": "dropdown", "default": "a", "options": ["a", "b"]},
        {"key": "password", "label": "Password", "type": "password", "default": ""},
    ]
    def teardown(ctx): ...

Plugins are found in (first id wins):
  - vacuula_server/plugins_builtin/*.py   shipped with vacuula
  - data/plugins/*.py or data/plugins/<id>/__init__.py   dropped in by the user
  - the "vacuula.plugins" entry point group   pip installed packages

setup() must return quickly; long running work belongs in a thread that exits when
ctx.stop_event is set. ctx.set_warning("...") shows a problem on the Plugins page while the
plugin keeps running (e.g. a server it talks to is unreachable), ctx.set_warning(None) clears it. Everything registered through ctx is undone when the plugin stops.
Enabled state and config are stored in data/plugins/plugins.json, every plugin starts disabled.
"""
from __future__ import annotations
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import pkgutil
import sys
import threading
import traceback
from pathlib import Path
from types import ModuleType
from collections.abc import Callable
from typing import Any

from vacuula_server import bus
from vacuula_server.constants import DATA_DIR
from vacuula_server.robot_commands import send_command

PLUGINS_DIR = DATA_DIR / "plugins"
SETTINGS_PATH = PLUGINS_DIR / "plugins.json"
ENTRY_POINT_GROUP = "vacuula.plugins"
CONFIG_TYPES = ("text", "number", "switch", "dropdown", "password")
# Stored passwords are never sent to the web app, it gets this instead and sends it back for "unchanged"
PASSWORD_MASK = "********"

logger = logging.getLogger(__name__)

RouteHandler = Callable[..., None]


class PluginContext:
    """Everything a plugin gets to talk to vacuula with."""

    def __init__(self, plugin: Plugin):
        self.plugin_id = plugin.id
        self.config: dict[str, Any] = dict(plugin.config)
        self.logger = logging.getLogger(f"vacuula_plugins.{plugin.id}")
        self.stop_event = threading.Event()
        self.warning: str | None = None
        self._subscriptions: list[bus.Subscription] = []
        self.routes: list[tuple[str, str, RouteHandler]] = []

    def set_warning(self, message: str | None, log: bool = True) -> None:
        """Show (or with None clear) a problem on the Plugins page while the plugin keeps running."""
        if message != self.warning:
            if message and log:
                self.logger.warning("%s", message)
            self.warning = message

    def subscribe(self, patterns: str | list[str], handler: bus.Handler) -> None:
        """Call handler(event, data) for bus events, see vacuula_server.bus for event names."""
        self._subscriptions.append(bus.subscribe(patterns, handler, f"plugin:{self.plugin_id}"))

    def add_route(self, method: str, pattern: str, handler: RouteHandler) -> None:
        """Serve handler(req, *groups) at /api/plugins/<id>/<pattern>, pattern is a regex."""
        self.routes.append((method.upper(), pattern.lstrip("/"), handler))

    def send_command(self, serial: str, cmd: dict, timeout: float | None = None) -> tuple[int, Any]:
        """Send a command to a robot and wait for the answer. Returns (http status, body)."""
        status, content = send_command(serial, cmd, timeout) if timeout else send_command(serial, cmd)
        if isinstance(content, (bytes, bytearray)):
            try:
                content = json.loads(content)
            except ValueError:
                pass
        return status, content

    def robots(self) -> dict:
        """Robot list with connection status, keyed by serial."""
        from vacuula_server.handlers.events import robots_snapshot
        return robots_snapshot()

    def _close(self) -> None:
        self.stop_event.set()
        for sub in self._subscriptions:
            sub.unsubscribe()
        self._subscriptions.clear()
        self.routes.clear()


class Plugin:
    def __init__(self, plugin_id: str, source: str, module: ModuleType | None = None, error: str | None = None):
        self.id = plugin_id
        self.source = source
        self.module = module
        self.enabled = False
        self.config: dict[str, Any] = {}
        self.ctx: PluginContext | None = None
        self.error = error

    @property
    def name(self) -> str:
        return str(getattr(self.module, "NAME", self.id))

    @property
    def description(self) -> str:
        return str(getattr(self.module, "DESCRIPTION", ""))

    @property
    def version(self) -> str:
        return str(getattr(self.module, "VERSION", ""))

    @property
    def config_schema(self) -> list[dict]:
        schema = getattr(self.module, "CONFIG", [])
        return [f for f in schema if isinstance(f, dict) and "key" in f and f.get("type", "text") in CONFIG_TYPES]

    @property
    def status(self) -> str:
        if self.ctx is not None:
            return "running"
        return "error" if self.error else "stopped"

    def defaults(self) -> dict[str, Any]:
        return {f["key"]: f.get("default") for f in self.config_schema}

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "loaded": self.module is not None,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
            "warning": self.ctx.warning if self.ctx else None,
            "config_schema": self.config_schema,
            "config": self.masked_config(),
        }

    def masked_config(self) -> dict[str, Any]:
        secrets = {f["key"] for f in self.config_schema if f.get("type") == "password"}
        return {k: (PASSWORD_MASK if v else "") if k in secrets else v for k, v in self.config.items()}


_plugins: dict[str, Plugin] = {}
_lock = threading.RLock()


def _coerce(field: dict, value: Any) -> Any:
    kind = field.get("type", "text")
    if kind == "switch":
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.get('label', field['key'])} must be a number")
        return int(number) if number.is_integer() else number
    if kind == "dropdown":
        options = field.get("options", [])
        if options and value not in options:
            raise ValueError(f"{field.get('label', field['key'])} must be one of {options}")
        return value
    return "" if value is None else str(value)


def _load_settings() -> dict:
    try:
        with SETTINGS_PATH.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Could not read %s, plugins start disabled: %s", SETTINGS_PATH, e)
        return {}


def _save_settings() -> None:
    data = {p.id: {"enabled": p.enabled, "config": p.config} for p in _plugins.values() if p.module is not None}
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(SETTINGS_PATH)


def _import_file(plugin_id: str, path: Path) -> ModuleType:
    name = f"vacuula_plugins.{plugin_id}"
    if path.is_dir():
        spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
    else:
        spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _candidates() -> list[tuple[str, str, Callable[[], ModuleType]]]:
    """(id, source, loader) for every plugin we can see, in priority order."""
    found: list[tuple[str, str, Callable[[], ModuleType]]] = []

    import vacuula_server.plugins_builtin as builtin
    for info in pkgutil.iter_modules(builtin.__path__):
        mod_name = f"{builtin.__name__}.{info.name}"
        found.append((info.name, "builtin", lambda m=mod_name: importlib.reload(sys.modules[m]) if m in sys.modules else importlib.import_module(m)))

    if PLUGINS_DIR.is_dir():
        for path in sorted(PLUGINS_DIR.iterdir()):
            if path.name.startswith((".", "_")):
                continue
            if path.is_file() and path.suffix == ".py":
                found.append((path.stem, "user", lambda p=path: _import_file(p.stem, p)))
            elif path.is_dir() and (path / "__init__.py").is_file():
                found.append((path.name, "user", lambda p=path: _import_file(p.name, p)))

    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        found.append((ep.name, "package", ep.load))

    return found


def _start(plugin: Plugin) -> None:
    if plugin.module is None or plugin.ctx is not None:
        return
    setup = getattr(plugin.module, "setup", None)
    if not callable(setup):
        plugin.error = "plugin has no setup(ctx) function"
        return
    ctx = PluginContext(plugin)
    try:
        setup(ctx)
    except Exception as e:
        ctx._close()
        plugin.error = f"setup failed: {type(e).__name__}: {e}"
        logger.error("Plugin %s %s\n%s", plugin.id, plugin.error, traceback.format_exc())
        return
    plugin.ctx = ctx
    plugin.error = None
    logger.info("Plugin %s started", plugin.id)


def _stop(plugin: Plugin) -> None:
    ctx = plugin.ctx
    if ctx is None:
        return
    plugin.ctx = None
    teardown = getattr(plugin.module, "teardown", None)
    if callable(teardown):
        try:
            teardown(ctx)
        except Exception:
            logger.exception("Plugin %s teardown failed", plugin.id)
    ctx._close()
    logger.info("Plugin %s stopped", plugin.id)


def load_all() -> None:
    """(Re)discover every plugin and start the enabled ones. Also used to pick up edited plugin files."""
    with _lock:
        for plugin in _plugins.values():
            _stop(plugin)
        _plugins.clear()

        settings = _load_settings()
        for plugin_id, source, load in _candidates():
            if plugin_id in _plugins:
                logger.warning("Plugin id %s from %s already taken, skipping", plugin_id, source)
                continue
            try:
                plugin = Plugin(plugin_id, source, load())
            except Exception as e:
                logger.error("Plugin %s failed to load: %s\n%s", plugin_id, e, traceback.format_exc())
                _plugins[plugin_id] = Plugin(plugin_id, source, error=f"load failed: {type(e).__name__}: {e}")
                continue

            saved = settings.get(plugin_id, {})
            plugin.config = {**plugin.defaults(), **{
                f["key"]: saved["config"][f["key"]]
                for f in plugin.config_schema
                if isinstance(saved.get("config"), dict) and f["key"] in saved["config"]
            }}
            plugin.enabled = bool(saved.get("enabled", False))
            _plugins[plugin_id] = plugin
            if plugin.enabled:
                _start(plugin)

        logger.info("Plugins loaded: %s", ", ".join(f"{p.id} ({p.status})" for p in _plugins.values()) or "none")


def list_plugins() -> list[dict]:
    with _lock:
        return [p.to_json() for p in _plugins.values()]


def update_plugin(plugin_id: str, enabled: bool | None = None, config: dict | None = None) -> dict:
    """Change enabled state and/or config. A running plugin is restarted to pick up new config.
    Raises KeyError for an unknown plugin, ValueError for invalid config."""
    with _lock:
        plugin = _plugins[plugin_id]
        if plugin.module is None:
            raise ValueError(plugin.error or "plugin failed to load")

        if config is not None:
            fields = {f["key"]: f for f in plugin.config_schema}
            plugin.config = {**plugin.config, **{
                k: _coerce(fields[k], v) for k, v in config.items()
                if k in fields and not (fields[k].get("type") == "password" and v == PASSWORD_MASK)
            }}
        if enabled is not None:
            plugin.enabled = enabled

        _stop(plugin)
        plugin.error = None
        if plugin.enabled:
            _start(plugin)
        _save_settings()
        return plugin.to_json()


def find_plugin_route(plugin_id: str, method: str, path: str) -> tuple[RouteHandler | None, list[str]]:
    from vacuula_server.handlers.route_matcher import find_route
    with _lock:
        plugin = _plugins.get(plugin_id)
        routes = list(plugin.ctx.routes) if plugin and plugin.ctx else []
    return find_route(routes, method, path)
