from __future__ import annotations
import logging
from typing import Any

from vacuula_server.conn import Gen2Gen3Conn
from vacuula_server.gen3.commands import send_chunk, build_command
from vacuula_server.gen4.handlers import robot_send_request
from vacuula_server.generation import Generation, get_robot_by_serial
from vacuula_server.state import robot_connections, pending_requests, PendingRequest

# Longest wait for a robot to answer a command before giving up
COMMAND_TIMEOUT_S = 30

logger = logging.getLogger(__name__)


def send_command(serial: str, cmd: dict, timeout: float = COMMAND_TIMEOUT_S) -> tuple[int, Any]:
    """Send a command to a connected robot and wait for its answer.

    Returns (status, content). On success content is the robot's answer (raw bytes for gen3,
    a dict for gen4) and status its result; on failure content is {"error": message}."""
    if get_robot_by_serial(serial) is None:
        return 404, {"error": "Robot not found"}

    if serial not in robot_connections:
        return 400, {"error": "Robot not connected"}

    conn = robot_connections[serial]
    slot = PendingRequest()
    if (conn.gen == Generation.GEN4):
        # gen4
        req_id, err = robot_send_request(conn, cmd, slot)
        if err:
            return 502, {"error": "Failed to send command to robot"}
    elif (conn.gen == Generation.GEN3) and isinstance(conn.conn, Gen2Gen3Conn):
        # gen3
        req_id, msg = build_command(serial, cmd)
        pending_requests[str(req_id)] = slot
        logger.debug("Sending command to robot %s: %s", serial, cmd)
        try:
            send_chunk(conn.conn.wfile, msg) # TODO
        except Exception:
            pending_requests.pop(str(req_id), None)
            return 502, {"error": "Failed to send command to robot"}
    else:
        return 400, {"error": "Unsupported robot generation"}

    key = str(req_id)
    slot.event.wait(timeout)
    pending_requests.pop(key, None)
    if slot.event.is_set():
        return slot.status, slot.content
    return 504, {"error": "Robot did not answer in time"}
