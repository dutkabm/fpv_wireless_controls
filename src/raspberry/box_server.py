#!/usr/bin/env python3
"""
HTTP JSON API for :class:`raspberry.box_control.BoxController` (LAN remote; used from ``ground_station.main`` Box tab).

From the repo root on the Pi::

    PYTHONPATH=src python3 -m raspberry.box_server

Environment:

- ``BOX_HTTP_BIND`` — listen address (default ``0.0.0.0``).
- ``BOX_HTTP_PORT`` — port (default ``50502``).

Bearer token for POST routes lives in process memory (``set_http_token``). ``raspberry.main``
generates one token, starts this server in a thread, and sends the same token in the TCP joystick handshake.

Routes (JSON):

- ``GET /api/status`` — open (no token); ``hardware_ok``, live fields from :class:`raspberry.models.SystemStatus`.
- ``POST /api/led`` — token required; body ``{"on": true|false}``.
- ``POST /api/servo`` — token required; body ``{"active": true|false}`` (false detaches PWM).
- ``POST /api/camera`` — token required; body ``{"streaming": true|false}`` (RTP/H264 UDP on port 5004).
- ``POST /api/drone-power`` — token required; body ``{"on": true|false}``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Optional, Tuple
from urllib.parse import urlparse

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

if __package__:
    from . import gpio_env  # noqa: F401 — before gpiozero
else:
    import gpio_env  # noqa: F401

from common.box_api import (
    API_CAMERA,
    API_DRONE_POWER,
    API_LED,
    API_SERVO,
    API_STATUS,
    BOX_HTTP_PORT,
)

if __package__:
    from .box_control import BoxController
else:
    from box_control import BoxController

_LOG = logging.getLogger(__name__)


class BoxServerState:
    """Process-wide box handle (lazy init, guarded by ``lock``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.box: Optional[BoxController] = None
        self.init_error: Optional[str] = None

    def get_box_locked(self) -> Tuple[Optional[BoxController], Optional[str]]:
        """Call only with ``self.lock`` held. Retries init each time until a box exists."""
        if self.box is not None:
            return self.box, None
        try:
            self.box = BoxController()
            self.init_error = None
            return self.box, None
        except Exception as e:
            self.init_error = str(e)
            _LOG.exception("BoxController init failed")
            return None, self.init_error

    def close_box_locked(self) -> None:
        if self.box is not None:
            try:
                self.box.close()
            except Exception:
                _LOG.exception("BoxController.close failed")
            self.box = None

    def init_at_startup(self) -> None:
        """Create BoxController at process start (sensor log, fail-fast before first client)."""
        with self.lock:
            _box, err = self.get_box_locked()
        if err is not None:
            _LOG.warning("Box hardware init failed at startup: %s", err)


STATE = BoxServerState()

_http_token: str = ""


def set_http_token(token: str) -> None:
    """Set bearer token for POST routes (called by ``raspberry.main`` before ``main()``)."""
    global _http_token
    _http_token = (token or "").strip()


def _auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    if not _http_token:
        return False
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {_http_token}":
        return True
    if handler.headers.get("X-Box-Token") == _http_token:
        return True
    return False


def _http_client_host(handler: BaseHTTPRequestHandler) -> str:
    """Client IPv4/IPv6 from the TCP connection (used as UDP stream destination)."""
    return handler.client_address[0]


def _note_stream_client(box: BoxController, handler: BaseHTTPRequestHandler) -> None:
    box.camera_stream.set_client_host(_http_client_host(handler))


def _read_json_body(handler: BaseHTTPRequestHandler, max_len: int = 4096) -> Any:
    n = handler.headers.get("Content-Length")
    if not n:
        return None
    try:
        ln = int(n)
    except ValueError:
        raise ValueError("bad Content-Length")
    if ln < 0 or ln > max_len:
        raise ValueError("body too large")
    raw = handler.rfile.read(ln)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


class BoxHTTPHandler(BaseHTTPRequestHandler):
    state: ClassVar[BoxServerState] = STATE
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOG.info("%s - " + fmt, self.address_string(), *args)

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code: int, msg: str) -> None:
        self._send_json(code, {"ok": False, "error": msg})

    def _crsf_fields(self) -> dict:
        try:
            from raspberry import crsf_bridge_state

            return crsf_bridge_state.snapshot()
        except Exception:
            _LOG.debug("CRSF bridge state unavailable", exc_info=True)
            return {}

    def _write_status_ok(self, box: BoxController) -> None:
        try:
            st = box.read_system_status()
        except (OSError, RuntimeError) as e:
            if isinstance(e, OSError):
                box.mark_sensor_failure("status", e)
            err = box.runtime_sensor_error or str(e)
            _LOG.warning("Sensor read failed; reporting hardware_ok=false (%s)", err)
            self._send_json(
                200,
                {
                    "ok": True,
                    "hardware_ok": False,
                    "hardware_error": err,
                    "sensors_ok": False,
                    **self._crsf_fields(),
                },
            )
            return
        d = asdict(st)
        sensors_ok = box.env is not None and box.batteries is not None
        d["hardware_ok"] = box.runtime_sensor_error is None
        d["sensors_ok"] = sensors_ok
        if box.runtime_sensor_error:
            d["hardware_error"] = box.runtime_sensor_error
        g = box.gpio
        if g.led_error:
            d["led_error"] = g.led_error
        if g.servo_error:
            d["servo_error"] = g.servo_error
        if g.drone_power_error:
            d["drone_power_error"] = g.drone_power_error
        d.update(self._crsf_fields())
        self._send_json(200, {"ok": True, **d})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != API_STATUS:
            self._fail(404, "not found")
            return
        with self.state.lock:
            box, err = self.state.get_box_locked()
            if box is None:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "hardware_ok": False,
                        "hardware_error": err or "unknown",
                        **self._crsf_fields(),
                    },
                )
                return
            if _auth_ok(self):
                _note_stream_client(box, self)
            self._write_status_ok(box)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not _auth_ok(self):
            self._fail(401, "unauthorized")
            return
        if path not in (API_LED, API_SERVO, API_CAMERA, API_DRONE_POWER):
            self._fail(404, "not found")
            return
        try:
            body = _read_json_body(self)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            self._fail(400, str(e))
            return
        if not isinstance(body, dict):
            self._fail(400, "JSON object required")
            return

        with self.state.lock:
            box, err = self.state.get_box_locked()
            if box is None:
                self._send_json(
                    503,
                    {"ok": False, "hardware_ok": False, "hardware_error": err or "unknown"},
                )
                return
            _note_stream_client(box, self)
            try:
                if path == API_LED:
                    if "on" not in body:
                        self._fail(400, "missing on")
                        return
                    box.gpio.led_set(bool(body["on"]))
                elif path == API_SERVO:
                    if "active" not in body:
                        self._fail(400, "missing active")
                        return
                    if bool(body["active"]):
                        pos = body.get("position", "neutral")
                        if pos == "neutral":
                            box.gpio.servo_start("neutral")
                        else:
                            box.gpio.servo_start(float(pos))
                    else:
                        box.gpio.servo_stop()
                elif path == API_CAMERA:
                    if "streaming" not in body:
                        self._fail(400, "missing streaming")
                        return
                    if bool(body["streaming"]):
                        ok = box.camera_stream_start(_http_client_host(self))
                        if not ok:
                            err_cam = box.camera_stream.last_error or "camera start failed"
                            self._send_json(
                                500,
                                {
                                    "ok": False,
                                    "error": err_cam,
                                    "camera_stream_error": err_cam,
                                },
                            )
                            return
                    else:
                        box.camera_stream_stop()
                elif path == API_DRONE_POWER:
                    if "on" not in body:
                        self._fail(400, "missing on")
                        return
                    box.gpio.drone_power_set(bool(body["on"]))
            except Exception as e:
                _LOG.exception("command failed")
                self._fail(500, str(e))
                return
            self._write_status_ok(box)


class _BoxHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    global _http_token
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not _http_token:
        _http_token = secrets.token_urlsafe(24)
        _LOG.info("Generated in-memory box HTTP token (standalone mode)")

    bind = os.environ.get("BOX_HTTP_BIND", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("BOX_HTTP_PORT", str(BOX_HTTP_PORT)))
    BoxHTTPHandler.state = STATE
    server = _BoxHTTPServer((bind, port), BoxHTTPHandler)

    def _shutdown(*_args: Any) -> None:
        _LOG.info("Shutting down…")
        with STATE.lock:
            STATE.close_box_locked()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    _LOG.info("Box HTTP API on http://%s:%s/ (GET %s)", bind, port, API_STATUS)
    STATE.init_at_startup()
    try:
        server.serve_forever()
    finally:
        with STATE.lock:
            STATE.close_box_locked()
        server.server_close()


if __name__ == "__main__":
    main()
