#!/usr/bin/env python3
"""
HTTP JSON API for :class:`raspberry.box_control.BoxController` (LAN remote; used from ``network_joystick_client`` Box tab).

Run from the repo root on the Pi::

    python3 -m raspberry.box_server

Environment:

- ``BOX_HTTP_BIND`` — listen address (default ``0.0.0.0``).
- ``BOX_HTTP_PORT`` — port (default ``50502``).
- ``BOX_HTTP_TOKEN`` — if set, require ``Authorization: Bearer <token>`` or ``X-Box-Token: <token>``.

Routes (JSON; path string constants in :mod:`raspberry.box_api_paths`):

- ``GET /api/status`` — ``hardware_ok``, live fields from :class:`raspberry.models.SystemStatus`,
  plus ``video_tcp_port`` for VLC / ffplay URLs.
- ``POST /api/led`` — body ``{"on": true|false}``.
- ``POST /api/servo`` — body ``{"active": true|false}`` (false detaches PWM).
- ``POST /api/camera`` — body ``{"streaming": true|false}``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, Optional, Tuple
from urllib.parse import urlparse

if __package__:
    from .box_control import BoxController
    from .video import camera_stream_tcp_port
else:
    from box_control import BoxController
    from video import camera_stream_tcp_port

_LOG = logging.getLogger(__name__)


API_STATUS = "/api/status"
API_LED = "/api/led"
API_SERVO = "/api/servo"
API_CAMERA = "/api/camera"


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


STATE = BoxServerState()


def _http_token() -> str:
    return os.environ.get("BOX_HTTP_TOKEN", "").strip()


def _auth_ok(handler: BaseHTTPRequestHandler) -> bool:
    tok = _http_token()
    if not tok:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {tok}":
        return True
    if handler.headers.get("X-Box-Token") == tok:
        return True
    return False


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

    def _write_status_ok(self, box: BoxController) -> None:
        st = box.read_system_status()
        d = asdict(st)
        d["video_tcp_port"] = camera_stream_tcp_port()
        d["hardware_ok"] = True
        d["sensors_ok"] = box.env is not None and box.batteries is not None
        g = box.gpio
        if g.led_error:
            d["led_error"] = g.led_error
        if g.servo_error:
            d["servo_error"] = g.servo_error
        if g.drone_power_error:
            d["drone_power_error"] = g.drone_power_error
        self._send_json(200, {"ok": True, **d})

    def do_GET(self) -> None:
        if not _auth_ok(self):
            self._fail(401, "unauthorized")
            return
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
                    },
                )
                return
            self._write_status_ok(box)

    def do_POST(self) -> None:
        if not _auth_ok(self):
            self._fail(401, "unauthorized")
            return
        path = urlparse(self.path).path
        if path not in (API_LED, API_SERVO, API_CAMERA):
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
                        ok = box.camera_stream_start()
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
            except Exception as e:
                _LOG.exception("command failed")
                self._fail(500, str(e))
                return
            self._write_status_ok(box)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    bind = os.environ.get("BOX_HTTP_BIND", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.environ.get("BOX_HTTP_PORT", "50502"))
    BoxHTTPHandler.state = STATE
    server = ThreadingHTTPServer((bind, port), BoxHTTPHandler)

    def _shutdown(*_args: Any) -> None:
        _LOG.info("Shutting down…")
        with STATE.lock:
            STATE.close_box_locked()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    _LOG.info("Box HTTP API on http://%s:%s/ (GET %s)", bind, port, API_STATUS)
    try:
        server.serve_forever()
    finally:
        with STATE.lock:
            STATE.close_box_locked()
        server.server_close()


if __name__ == "__main__":
    main()
