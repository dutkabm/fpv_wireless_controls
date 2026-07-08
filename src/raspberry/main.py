#!/usr/bin/env python3
"""
Raspberry Pi TX bridge: receive joystick channel frames over UDP, forward CRSF to the TX module.

From the repo root::

    PYTHONPATH=src python3 -m raspberry.main

Serial settings default from ``src/ground_station/controller_map.txt`` on the Pi if present (``--config``).
Starts ``raspberry.box_server`` in-process (shared box HTTP token in handshake).
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import logging
import os
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import serial

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from common.crsf import pwm_channels_to_crsf_packet
from common.network import (
    CHANNEL_PACKET_MAGIC,
    CHANNEL_PAYLOAD_LEN,
    DEFAULT_HANDSHAKE_TCP_PORT,
    DEFAULT_UDP_CHANNEL_PORT,
    format_handshake_ok,
    unpack_channel_datagram,
)
from common.tx_port import autodetect_serial_port, is_autoselect_serial_port

log = logging.getLogger(__name__)


def _rx_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_UDP_RECV_MAX = 2048
_DEFAULT_BRIDGE_CONFIG = str(_SRC_ROOT / "ground_station" / "controller_map.txt")


def _strip_inline_comment(value: Optional[str]) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def load_serial_from_config(config_path: str) -> tuple[str, int]:
    default_port = "AUTO"
    default_baud = 400000
    if not os.path.exists(config_path):
        return default_port, default_baud
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    if "General" not in cfg:
        return default_port, default_baud
    g = cfg["General"]
    port = _strip_inline_comment(g.get("serial_port", fallback=default_port)).strip()
    baud_raw = _strip_inline_comment(g.get("baud_rate", fallback=str(default_baud)))
    try:
        baud = int(baud_raw)
    except ValueError:
        baud = default_baud
    return port, baud


def _tcp_port_available(bind: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((bind, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _start_box_http_server(token: str) -> None:
    """Run ``box_server`` in-process so it shares the in-memory token."""
    from raspberry import box_server as box_server_mod

    bind = "0.0.0.0"
    port = box_server_mod.BOX_HTTP_PORT
    if not _tcp_port_available(bind, port):
        log.error(
            "Box HTTP port %s is already in use. Stop any other box_server or tx_bridge "
            "(e.g. pkill -f box_server) and restart — only one listener is supported.",
            port,
        )
        return

    box_server_mod.set_http_token(token)

    def _run_box_http() -> None:
        try:
            box_server_mod.main()
        except OSError as e:
            log.error("Box HTTP server failed: %s", e)

    threading.Thread(target=_run_box_http, daemon=True, name="box-http").start()
    log.info("Box HTTP API thread started on port %s", port)


def main() -> None:
    ap = argparse.ArgumentParser(description="UDP → CRSF serial bridge for Pi + TX module")
    ap.add_argument("--bind", default="0.0.0.0", help="UDP / TCP bind address")
    ap.add_argument(
        "--port",
        type=int,
        default=DEFAULT_UDP_CHANNEL_PORT,
        help=f"UDP port for channel frames (default {DEFAULT_UDP_CHANNEL_PORT})",
    )
    ap.add_argument(
        "--handshake-port",
        type=int,
        default=DEFAULT_HANDSHAKE_TCP_PORT,
        help=f"TCP port for client Connect handshake (default {DEFAULT_HANDSHAKE_TCP_PORT})",
    )
    ap.add_argument(
        "--serial",
        default=None,
        help="Serial device (default: AUTO or from controller_map.txt General.serial_port)",
    )
    ap.add_argument("--baud", type=int, default=None, help="Baud rate (default from controller_map.txt or 400000)")
    ap.add_argument(
        "--config",
        default=_DEFAULT_BRIDGE_CONFIG,
        help="INI file path for baud/serial hints (controller_map.txt)",
    )
    ap.add_argument("--hz", type=float, default=50.0, help="CRSF transmit rate toward TX")
    ap.add_argument("--failsafe-ms", type=float, default=500.0, help="Hold last channels; fail-safe defaults after this latency")
    ap.add_argument(
        "--name",
        default=None,
        help="Identifier sent to clients during TCP handshake (default: login user name)",
    )
    ap.add_argument("--debug", action="store_true", help="Log each valid UDP joystick packet at DEBUG")
    args = ap.parse_args()

    bridge_name = (args.name or "").strip() or getpass.getuser()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    box_http_token = secrets.token_urlsafe(24)
    _start_box_http_server(box_http_token)

    cfg_serial, cfg_baud = load_serial_from_config(args.config)
    serial_port_pref = args.serial if args.serial is not None else cfg_serial
    baud_rate = args.baud if args.baud is not None else cfg_baud

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.setblocking(False)

    handshake_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    handshake_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    handshake_srv.bind((args.bind, args.handshake_port))
    handshake_srv.listen(8)
    handshake_srv.settimeout(1.0)

    def handshake_loop() -> None:
        while True:
            try:
                conn, addr = handshake_srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            log.info("TCP handshake: client %s:%s connected", addr[0], addr[1])
            try:
                conn.settimeout(5.0)
                data = conn.recv(64)
                if data and data.strip() == CHANNEL_PACKET_MAGIC:
                    conn.sendall(format_handshake_ok(bridge_name, box_http_token))
                    log.info(
                        "TCP handshake: OK (%r) sent to %s:%s",
                        bridge_name,
                        addr[0],
                        addr[1],
                    )
                else:
                    log.warning(
                        "TCP handshake: bad request from %s:%s (%r)",
                        addr[0],
                        addr[1],
                        data,
                    )
            except OSError as e:
                log.warning("TCP handshake: error with %s:%s: %s", addr[0], addr[1], e)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    hs = threading.Thread(target=handshake_loop, daemon=True)
    hs.start()

    print(
        f"UDP {args.bind}:{args.port} · TCP handshake {args.bind}:{args.handshake_port} "
        f"as {bridge_name!r} ({args.hz:.0f} Hz CRSF). Serial preference: {serial_port_pref!r}"
    )
    if args.debug:
        log.info("UDP joystick: DEBUG log line per valid packet (--debug)")

    period = 1.0 / max(args.hz, 1.0)
    fail_ns = int(max(args.failsafe_ms, 0.0) * 1e9)
    serial_retry_period_s = 2.0

    ser: Optional[serial.Serial] = None
    current_serial_path: Optional[str] = None
    last_serial_attempt_s = 0.0
    waiting_announced = False

    def try_open_serial() -> None:
        nonlocal ser, current_serial_path, waiting_announced
        resolved = autodetect_serial_port(baud_rate, serial_port_pref)
        if resolved is None:
            if not waiting_announced:
                log.info(
                    "No TX USB-UART detected yet (Linux: ttyACM*/ttyUSB*; macOS: cu.usbserial* / cu.usbmodem*; "
                    "preference %r). UDP/TCP listeners are up; will keep scanning.",
                    serial_port_pref,
                )
                waiting_announced = True
            return
        try:
            new_ser = serial.Serial(resolved, baud_rate, timeout=0)
        except (serial.SerialException, OSError) as e:
            log.warning("Could not open serial %s: %s; will retry.", resolved, e)
            return
        ser = new_ser
        current_serial_path = resolved
        waiting_announced = False
        if is_autoselect_serial_port(serial_port_pref):
            log.info("Serial open: %s @ %d (auto-detected).", resolved, baud_rate)
        elif str(serial_port_pref).strip() != str(resolved).strip():
            log.info(
                "Serial open: %s @ %d (configured %r unavailable; using detected).",
                resolved,
                baud_rate,
                serial_port_pref,
            )
        else:
            log.info("Serial open: %s @ %d.", resolved, baud_rate)

    try_open_serial()
    last_serial_attempt_s = time.monotonic()

    lock = threading.Lock()
    latest: Optional[List[int]] = None
    last_rx = 0

    def recv_loop() -> None:
        nonlocal latest, last_rx
        while True:
            try:
                data, addr = sock.recvfrom(_UDP_RECV_MAX)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            except OSError:
                time.sleep(0.01)
                continue
            if len(data) != CHANNEL_PAYLOAD_LEN:
                log.debug(
                    "[%s] UDP from %s:%s ignored: length=%s (want %s) head=%r",
                    _rx_utc_iso(),
                    addr[0],
                    addr[1],
                    len(data),
                    CHANNEL_PAYLOAD_LEN,
                    data[:8],
                )
                continue
            parsed = unpack_channel_datagram(data)
            if parsed is None:
                log.debug(
                    "[%s] UDP from %s:%s ignored: expected magic %r head=%r",
                    _rx_utc_iso(),
                    addr[0],
                    addr[1],
                    CHANNEL_PACKET_MAGIC,
                    data[:8],
                )
                continue
            log.debug(
                "[%s] UDP %s:%s channels=%s",
                _rx_utc_iso(),
                addr[0],
                addr[1],
                parsed,
            )
            with lock:
                latest = parsed
                last_rx = time.monotonic_ns()

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    failsafe_pwm = [1500] * 16
    try:
        while True:
            if ser is None:
                now_s = time.monotonic()
                if now_s - last_serial_attempt_s >= serial_retry_period_s:
                    last_serial_attempt_s = now_s
                    try_open_serial()
                time.sleep(period)
                continue

            now = time.monotonic_ns()
            with lock:
                ch = list(latest) if latest is not None else None
                stale = (now - last_rx) > fail_ns if fail_ns > 0 else False
            if ch is None or stale:
                ch = failsafe_pwm
            try:
                ser.write(pwm_channels_to_crsf_packet(ch))
            except (serial.SerialException, OSError) as e:
                log.warning(
                    "Serial write failed on %s (%s); closing and re-scanning.",
                    current_serial_path,
                    e,
                )
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                current_serial_path = None
                last_serial_attempt_s = time.monotonic()
                continue
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        try:
            handshake_srv.close()
        except OSError:
            pass
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        sock.close()


if __name__ == "__main__":
    main()
