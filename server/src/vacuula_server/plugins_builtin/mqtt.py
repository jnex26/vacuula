"""Publish robots to an MQTT broker, with Home Assistant MQTT discovery.

Every robot becomes a Home Assistant vacuum (start, pause, stop, return to base, locate,
spot clean on gen3, fan speed) plus battery and charging sensors. Without Home Assistant
the topics are still useful on their own:

  <base>/status                  online / offline (retained, last will)
  <base>/<serial>/availability   online / offline (retained), robot connected to vacuula
  <base>/<serial>/state          {"state": "docked|cleaning|paused|idle|returning|error", "fan_speed": ...}
  <base>/<serial>/attributes     battery, charging, docked, action, error, alert, ...
  <base>/<serial>/command        start | pause | stop | return_to_base | clean_spot | locate
  <base>/<serial>/fan_speed/set  one of the robot's fan speeds, used for the next start
"""
from __future__ import annotations
import json
import re
import threading

import paho.mqtt.client as mqtt

from vacuula_server.constants import APP_VERSION

NAME = "MQTT / Home Assistant"
DESCRIPTION = "Publish robots to an MQTT broker, with Home Assistant discovery: every robot becomes a vacuum entity with battery and charging sensors."
VERSION = "1.0.0"
CONFIG = [
    {"key": "host", "label": "Broker host", "type": "text", "default": ""},
    {"key": "port", "label": "Broker port", "type": "number", "default": 1883},
    {"key": "username", "label": "Username", "type": "text", "default": ""},
    {"key": "password", "label": "Password", "type": "password", "default": ""},
    {"key": "base_topic", "label": "Base topic", "type": "text", "default": "vacuula"},
    {"key": "discovery", "label": "Home Assistant discovery", "type": "switch", "default": True},
    {"key": "discovery_prefix", "label": "Discovery prefix", "type": "text", "default": "homeassistant"},
    {"key": "refresh", "label": "State refresh interval (seconds, 0 = events only)", "type": "number", "default": 60},
    {"key": "gen3_category", "label": "Gen3 start cleaning", "type": "dropdown", "default": "house", "options": ["house", "map"]},
]

SUPPORT_URL = "https://github.com/vacuula/vacuula"

# gen3 robot state / action codes, see app/src/types/neato/gen3/state.ts
GEN3_STATES = {1: "idle", 2: "busy", 3: "paused", 4: "error"}
GEN3_ACTION_DOCKING = 4
GEN3_CATEGORIES = {"house": 2, "spot": 3, "map": 4}
GEN3_FAN_SPEEDS = {"eco": 1, "normal": 2}
GEN3_SPOT_SIZE_CM = 200
GEN4_FAN_SPEEDS = ["eco", "turbo"]

COMMANDS = ("start", "pause", "stop", "return_to_base", "clean_spot", "locate")


def _object_id(serial: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", serial.lower())


def robot_status(state: dict) -> dict | None:
    """Normalize a gen3 or gen4 robot state into what we publish, None if it isn't a robot state."""
    raw_state = state.get("state")
    details = state.get("details") or {}
    if isinstance(raw_state, int):  # gen3
        status = GEN3_STATES.get(raw_state)
        returning = state.get("action") == GEN3_ACTION_DOCKING
        docked = bool(details.get("isDocked"))
        charging = bool(details.get("isCharging"))
        error = state.get("error")
    elif isinstance(raw_state, str):  # gen4
        status = raw_state
        returning = state.get("action") == "docking"
        docked = bool(details.get("is_docked"))
        charging = bool(details.get("is_charging"))
        errors = state.get("errors") or []
        error = ", ".join(e.get("code", "") for e in errors if isinstance(e, dict)) or None
    else:
        return None

    if status == "error":
        ha_state = "error"
    elif status == "paused":
        ha_state = "paused"
    elif status == "busy":
        ha_state = "returning" if returning else "cleaning"
    else:
        ha_state = "docked" if docked else "idle"

    return {
        "state": ha_state,
        "battery": details.get("charge"),
        "charging": charging,
        "docked": docked,
        "action": state.get("action"),
        "error": error,
        "alert": state.get("alert"),
    }


def state_from_event(data: dict) -> dict | None:
    """The robot state inside a robot.event, if it carries one."""
    message = data.get("data") or {}
    if data.get("type") == "neato.gen3" and message.get("type") == "state":
        return message.get("data")
    if data.get("type") == "neato.gen4" and (message.get("to") or {}).get("service") == "peer.state":
        return message.get("body")
    return None


def state_from_response(robot_type: str, content) -> dict | None:
    if not isinstance(content, dict):
        return None
    return content if robot_type == "neato.gen3" else content.get("body")


class Bridge:
    def __init__(self, ctx):
        self.ctx = ctx
        self.config = ctx.config
        self.base = self.config["base_topic"].strip().strip("/") or "vacuula"
        self.prefix = self.config["discovery_prefix"].strip().strip("/") or "homeassistant"
        self.robots: dict[str, dict] = {}       # serial -> robots_snapshot() entry
        self.status: dict[str, dict] = {}       # serial -> last published robot_status()
        self.fan_speed: dict[str, str] = {}     # serial -> fan speed for the next start
        self.lock = threading.Lock()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"vacuula-{ctx.plugin_id}")
        if self.config["username"]:
            self.client.username_pw_set(self.config["username"], self.config["password"] or None)
        self.client.will_set(self.topic("status"), "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    @property
    def broker(self) -> str:
        return f"{self.config['host'].strip()}:{int(self.config['port'])}"

    def topic(self, *parts: str) -> str:
        return "/".join((self.base, *parts))

    def publish(self, topic: str, payload, retain: bool = True) -> None:
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=1, retain=retain)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for serial, info in self.ctx.robots().items():
            self.robots[serial] = info
        self.ctx.logger.info("Connecting to MQTT broker %s", self.broker)
        self.ctx.set_warning(f"Connecting to MQTT broker {self.broker}", log=False)
        self.client.connect_async(self.config["host"].strip(), int(self.config["port"]), keepalive=60)
        self.client.loop_start()
        self.ctx.subscribe("robot.*", self.on_bus_event)
        if float(self.config["refresh"]) > 0:
            threading.Thread(target=self.refresh_loop, name="mqtt-refresh", daemon=True).start()

    def stop(self) -> None:
        if self.client.is_connected():
            self.client.publish(self.topic("status"), "offline", qos=1, retain=True).wait_for_publish(2)
        self.client.disconnect()
        self.client.loop_stop()

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self.ctx.set_warning(f"MQTT broker {self.broker} refused the connection: {reason_code}")
            return
        self.ctx.set_warning(None)
        self.ctx.logger.info("Connected to MQTT broker %s", self.broker)
        client.subscribe([(self.topic("+", "command"), 1), (self.topic("+", "fan_speed", "set"), 1)])
        if self.config["discovery"]:
            client.subscribe(f"{self.prefix}/status", 1)
        self.publish(self.topic("status"), "online")
        for serial in list(self.robots):
            self.announce(serial)
            self.request_state(serial)

    def on_connect_fail(self, client, userdata) -> None:
        self.ctx.set_warning(f"Cannot reach MQTT broker {self.broker}, retrying. Check the host and port")

    def on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code != 0 and not self.ctx.stop_event.is_set():
            self.ctx.set_warning(f"Lost connection to MQTT broker {self.broker} ({reason_code}), reconnecting")

    def refresh_loop(self) -> None:
        interval = max(5.0, float(self.config["refresh"]))
        while not self.ctx.stop_event.wait(interval):
            for serial, info in list(self.robots.items()):
                if info.get("connected"):
                    self.request_state(serial, background=False)

    # --- robots ------------------------------------------------------------

    def fan_speeds(self, serial: str) -> list[str]:
        return list(GEN3_FAN_SPEEDS) if self.robots[serial].get("type") == "neato.gen3" else GEN4_FAN_SPEEDS

    def announce(self, serial: str) -> None:
        """Publish availability and (if enabled) Home Assistant discovery for one robot."""
        info = self.robots[serial]
        self.publish(self.topic(serial, "availability"), "online" if info.get("connected") else "offline")
        if not self.config["discovery"]:
            return

        oid = _object_id(serial)
        is_gen3 = info.get("type") == "neato.gen3"
        device = {
            "identifiers": [f"vacuula_{oid}"],
            "name": info.get("name") or serial,
            "manufacturer": "Neato",
            "model": "Gen3 (D3-D7)" if is_gen3 else "Gen4 (D800, D8-D10)",
            "serial_number": info.get("label_serial") or serial,
        }
        common = {
            "device": device,
            "origin": {"name": "Vacuula", "sw_version": APP_VERSION, "support_url": SUPPORT_URL},
            "availability": [
                {"topic": self.topic("status")},
                {"topic": self.topic(serial, "availability")},
            ],
            "availability_mode": "all",
        }
        features = ["start", "pause", "stop", "return_home", "status", "locate", "fan_speed"]
        if is_gen3:
            features.append("clean_spot")

        self.publish(f"{self.prefix}/vacuum/vacuula_{oid}/config", {
            **common,
            "name": None,
            "unique_id": f"vacuula_{oid}",
            "state_topic": self.topic(serial, "state"),
            "json_attributes_topic": self.topic(serial, "attributes"),
            "command_topic": self.topic(serial, "command"),
            "set_fan_speed_topic": self.topic(serial, "fan_speed", "set"),
            "fan_speed_list": self.fan_speeds(serial),
            "supported_features": features,
        })
        self.publish(f"{self.prefix}/sensor/vacuula_{oid}_battery/config", {
            **common,
            "name": "Battery",
            "unique_id": f"vacuula_{oid}_battery",
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "state_topic": self.topic(serial, "attributes"),
            "value_template": "{{ value_json.battery }}",
        })
        self.publish(f"{self.prefix}/binary_sensor/vacuula_{oid}_charging/config", {
            **common,
            "name": "Charging",
            "unique_id": f"vacuula_{oid}_charging",
            "device_class": "battery_charging",
            "state_topic": self.topic(serial, "attributes"),
            "value_template": "{{ 'ON' if value_json.charging else 'OFF' }}",
        })

    def publish_status(self, serial: str, state: dict) -> None:
        status = robot_status(state)
        if status is None or serial not in self.robots:
            return
        with self.lock:
            self.status[serial] = status
            fan_speed = self.fan_speed.setdefault(serial, self.fan_speeds(serial)[0])
        info = self.robots[serial]
        self.publish(self.topic(serial, "state"), {"state": status["state"], "fan_speed": fan_speed})
        self.publish(self.topic(serial, "attributes"), {
            **status,
            "name": info.get("name"),
            "serial": serial,
            "generation": info.get("type"),
        })

    def request_state(self, serial: str, background: bool = True) -> None:
        if background:
            threading.Thread(target=self.request_state, args=(serial, False), daemon=True).start()
            return
        robot_type = self.robots.get(serial, {}).get("type", "")
        cmd = {"cmd": "getRobotState"} if robot_type == "neato.gen3" else {"ability": "state.show"}
        status, content = self.ctx.send_command(serial, cmd)
        if status == 200 and (state := state_from_response(robot_type, content)):
            self.publish_status(serial, state)

    # --- events from vacuula -----------------------------------------------

    def on_bus_event(self, event: str, data: dict) -> None:
        serial = data["serial"]
        if event in ("robot.connected", "robot.disconnected", "robot.renamed"):
            self.robots = dict(self.ctx.robots())
            if serial in self.robots and self.client.is_connected():
                self.announce(serial)
                if event == "robot.connected":
                    self.request_state(serial)
        elif event == "robot.event":
            if (state := state_from_event(data)) is not None:
                self.publish_status(serial, state)

    # --- commands from MQTT --------------------------------------------------

    def on_message(self, client, userdata, message) -> None:
        payload = message.payload.decode(errors="replace").strip()
        if message.topic == f"{self.prefix}/status":
            if payload == "online":  # Home Assistant restarted
                for serial in list(self.robots):
                    self.announce(serial)
            return

        parts = message.topic[len(self.base) + 1:].split("/")
        serial = parts[0]
        if serial not in self.robots:
            self.ctx.logger.warning("MQTT command for unknown robot %s", serial)
            return
        if parts[1:] == ["fan_speed", "set"]:
            self.set_fan_speed(serial, payload)
        elif parts[1:] == ["command"]:
            threading.Thread(target=self.run_command, args=(serial, payload), daemon=True).start()

    def set_fan_speed(self, serial: str, fan_speed: str) -> None:
        if fan_speed not in self.fan_speeds(serial):
            self.ctx.logger.warning("Unknown fan speed %r for %s", fan_speed, serial)
            return
        with self.lock:
            self.fan_speed[serial] = fan_speed
            status = self.status.get(serial)
        if status:
            self.publish(self.topic(serial, "state"), {"state": status["state"], "fan_speed": fan_speed})

    def robot_command(self, serial: str, command: str) -> dict | None:
        is_gen3 = self.robots[serial].get("type") == "neato.gen3"
        with self.lock:
            paused = self.status.get(serial, {}).get("state") == "paused"
            fan_speed = self.fan_speed.get(serial, self.fan_speeds(serial)[0])

        if command == "start" and paused:
            return {"cmd": "resumeCleaning"} if is_gen3 else {"ability": "cleaning.resume"}
        if command == "start" and is_gen3:
            category = GEN3_CATEGORIES.get(self.config["gen3_category"], GEN3_CATEGORIES["house"])
            return {"cmd": "startCleaning", "params": {"category": category, "mode": GEN3_FAN_SPEEDS[fan_speed], "navigationMode": 1}}
        if command == "start":
            return {"ability": "cleaning.start", "runs": [{"map": None, "settings": {"mode": fan_speed, "navigation_mode": "normal"}}]}
        if command == "clean_spot" and is_gen3:
            return {"cmd": "startCleaning", "params": {
                "category": GEN3_CATEGORIES["spot"], "mode": GEN3_FAN_SPEEDS[fan_speed], "navigationMode": 1,
                "spotWidth": GEN3_SPOT_SIZE_CM, "spotHeight": GEN3_SPOT_SIZE_CM,
            }}
        gen3 = {"pause": "pauseCleaning", "stop": "stopCleaning", "return_to_base": "sendToBase", "locate": "findMe"}
        gen4 = {"pause": "cleaning.pause", "stop": "cleaning.cancel", "return_to_base": "navigation.return_to_base", "locate": "utilities.find_me"}
        if is_gen3 and command in gen3:
            return {"cmd": gen3[command]}
        if not is_gen3 and command in gen4:
            return {"ability": gen4[command]}
        return None

    def run_command(self, serial: str, command: str) -> None:
        cmd = self.robot_command(serial, command)
        if cmd is None:
            self.ctx.logger.warning("Unsupported command %r for %s", command, serial)
            return
        status, content = self.ctx.send_command(serial, cmd)
        if status != 200:
            self.ctx.logger.warning("Command %s for %s failed: %s %s", command, serial, status, content)
        self.request_state(serial, background=False)


def setup(ctx):
    if not ctx.config["host"].strip():
        raise ValueError("Broker host is required")
    if ctx.config["gen3_category"] not in GEN3_CATEGORIES:
        raise ValueError("Gen3 start cleaning must be house or map")
    bridge = Bridge(ctx)
    ctx.bridge = bridge
    bridge.start()


def teardown(ctx):
    bridge = getattr(ctx, "bridge", None)
    if bridge is not None:
        bridge.stop()
