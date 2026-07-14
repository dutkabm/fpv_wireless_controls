#!/usr/bin/env python3
"""
Raspberry Pi TX bridge: receive joystick channel frames over UDP, forward CRSF
to either a USB TX module or a direct FC UART (``crsf_output`` mode).

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
from typing import List, Optional, Tuple

import serial

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from raspberry import gpio_env  # noqa: F401 — before gpiozero (box_server thread)
from raspberry.crsf_output import (
    CRSF_OUTPUT_TX,
    CRSF_OUTPUT_UART,
    DEFAULT_UART_PORT,
    baud_for_crsf_output,
    normalize_crsf_output_mode,
    resolve_uart_port,
)

from common.crsf import CrsfSerialReader, pwm_channels_to_crsf_packet
from common.network import (
    CHANNEL_PACKET_MAGIC,
    CHANNEL_PAYLOAD_LEN,
    DEFAULT_HANDSHAKE_TCP_PORT,
    DEFAULT_UDP_CHANNEL_PORT,
    format_handshake_ok,
    unpack_channel_datagram,
)
from common.tx_port import is_autoselect_serial_port, resolve_crsf_serial_port
from raspberry import crsf_bridge_state

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


def load_serial_from_config(config_path: str) -> Tuple[str, str, str]:
    """Return (tx_serial_pref, crsf_output mode, uart_port). Baud is fixed per mode."""
    default_port = "AUTO"
    default_mode = CRSF_OUTPUT_UART
    default_uart = DEFAULT_UART_PORT
    if not os.path.exists(config_path):
        return default_port, default_mode, default_uart
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    if "General" not in cfg:
        return default_port, default_mode, default_uart
    g = cfg["General"]
    port = _strip_inline_comment(g.get("serial_port", fallback=default_port)).strip()
    mode = normalize_crsf_output_mode(
        _strip_inline_comment(g.get("crsf_output", fallback=default_mode)),
        default=default_mode,
    )
    uart = resolve_uart_port(
        _strip_inline_comment(g.get("uart_port", fallback=default_uart)),
        default=default_uart,
    )
    return port, mode, uart


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
    ap = argparse.ArgumentParser(
        description="UDP → CRSF serial bridge (TX module USB or direct FC UART)"
    )
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
        help="TX-module serial device (default: AUTO or from controller_map.txt General.serial_port)",
    )
    ap.add_argument(
        "--output",
        choices=(CRSF_OUTPUT_TX, CRSF_OUTPUT_UART),
        default=None,
        help="CRSF output: tx=USB TX module, uart=Pi UART to FC (default from config or uart)",
    )
    ap.add_argument(
        "--uart",
        default=None,
        help=f"Direct FC UART device when --output uart (default Pi primary UART: {DEFAULT_UART_PORT})",
    )
    ap.add_argument(
        "--config",
        default=_DEFAULT_BRIDGE_CONFIG,
        help="INI file path for serial/output hints (controller_map.txt)",
    )
    ap.add_argument("--hz", type=float, default=50.0, help="CRSF transmit rate")
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

    cfg_serial, cfg_mode, cfg_uart = load_serial_from_config(args.config)
    serial_port_pref = args.serial if args.serial is not None else cfg_serial
    output_mode = args.output if args.output is not None else cfg_mode
    baud_rate = baud_for_crsf_output(output_mode)
    uart_port = resolve_uart_port(
        args.uart if args.uart is not None else cfg_uart,
        default=DEFAULT_UART_PORT,
    )

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
        f"as {bridge_name!r} ({args.hz:.0f} Hz CRSF). "
        f"Output: {output_mode} @ {baud_rate} baud "
        f"(tx pref {serial_port_pref!r}, uart {uart_port!r})"
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
    crsf_reader = CrsfSerialReader()
    crsf_bridge_state.set_output_mode(output_mode)
    crsf_bridge_state.set_serial(open_=False)

    def _publish_telemetry() -> None:
        crsf_bridge_state.update_telemetry(crsf_reader.telemetry)

    def close_serial() -> None:
        nonlocal ser, current_serial_path, waiting_announced
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        ser = None
        current_serial_path = None
        waiting_announced = False
        crsf_reader.telemetry.clear()
        crsf_bridge_state.set_serial(open_=False)
        _publish_telemetry()

    def try_open_serial() -> None:
        nonlocal ser, current_serial_path, waiting_announced
        resolved = resolve_crsf_serial_port(
            baud_rate,
            mode=output_mode,
            tx_serial_pref=serial_port_pref,
            uart_port=uart_port,
        )
        if resolved is None:
            if not waiting_announced:
                if output_mode == CRSF_OUTPUT_UART:
                    log.info(
                        "Direct UART mode: no Pi UART device yet "
                        "(tried serial0/ttyAMA0/ttyS0/ttyAMA10; preference %r). "
                        "Enable serial hardware (raspi-config → Interface → Serial: login shell No, "
                        "serial port Yes; on Pi 5 also dtparam=uart0 in /boot/firmware/config.txt). "
                        "UDP/TCP listeners are up; will keep retrying.",
                        uart_port,
                    )
                else:
                    log.info(
                        "No TX USB-UART detected yet (Linux: ttyACM*/ttyUSB*; macOS: cu.usbserial* / cu.usbmodem*; "
                        "preference %r). UDP/TCP listeners are up; will keep scanning.",
                        serial_port_pref,
                    )
                waiting_announced = True
            return
        try:
            new_ser = serial.Serial(
                resolved,
                baud_rate,
                timeout=0,
                write_timeout=0,
                inter_byte_timeout=None,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # USB-CDC adapters often mute RX until DTR/RTS are asserted.
            for attr, val in (("dtr", True), ("rts", True)):
                try:
                    setattr(new_ser, attr, val)
                except Exception:
                    pass
        except (serial.SerialException, OSError) as e:
            hint = ""
            if output_mode == CRSF_OUTPUT_UART and getattr(e, "errno", None) == 2:
                hint = (
                    " Enable UART (raspi-config serial hardware) or set uart_port to an existing device."
                )
            log.warning("Could not open serial %s: %s;%s will retry.", resolved, e, hint)
            return
        ser = new_ser
        current_serial_path = resolved
        waiting_announced = False
        crsf_reader.telemetry.clear()
        crsf_reader.reset_stats()
        crsf_bridge_state.set_serial(open_=True, path=resolved)
        _publish_telemetry()
        if output_mode == CRSF_OUTPUT_UART:
            log.info(
                "Serial open: %s @ %d (Pi emulates CRSF RX → FC). "
                "Baud must match the FC CRSF port (configured %d).",
                resolved,
                baud_rate,
                baud_rate,
            )
            log.info(
                "Wire Pi TX→FC RX and Pi RX←FC TX (full duplex). "
                "Expect battery/GPS/attitude from FC — not RF LQ (no radio link)."
            )
        elif is_autoselect_serial_port(serial_port_pref):
            log.info("Serial open: %s @ %d (TX module auto-detected).", resolved, baud_rate)
            log.info(
                "CRSF link stats need bidirectional USB CRSF "
                "(ELRS /hardware.html RX=3 TX=1, or FTDI inverted half-duplex with RX tied)."
            )
        elif str(serial_port_pref).strip() != str(resolved).strip():
            log.info(
                "Serial open: %s @ %d (configured %r unavailable; using detected).",
                resolved,
                baud_rate,
                serial_port_pref,
            )
        else:
            log.info("Serial open: %s @ %d (TX module).", resolved, baud_rate)
        log.info(
            "CRSF telemetry RX enabled on %s mode=%s (use --debug for per-frame logs)",
            resolved,
            output_mode,
        )

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
    last_telem_log_s = 0.0
    last_link_ok: Optional[bool] = None
    bytes_rx_total = 0
    first_rx_logged = False
    # Drain RX even when in_waiting is unreliable (some USB-UART adapters).
    _RX_CHUNK = 512

    def _log_telem_pulse(*, force: bool = False) -> None:
        nonlocal last_telem_log_s, last_link_ok
        now_s = time.monotonic()
        snap = crsf_bridge_state.snapshot()
        link_ok = bool(snap.get("crsf_link_ok"))
        if last_link_ok is None or link_ok != last_link_ok:
            last_link_ok = link_ok
            telem = snap.get("crsf_telemetry") or {}
            log.info(
                "CRSF link %s (serial=%s path=%s mode=%s fc_ok=%s rf_ok=%s LQ=%s keys=%s)",
                "OK" if link_ok else "down",
                snap.get("crsf_serial_open"),
                snap.get("crsf_serial_path") or "—",
                snap.get("crsf_output") or "—",
                snap.get("crsf_fc_ok"),
                snap.get("crsf_rf_link_ok"),
                telem.get("Uplink LQ", "—"),
                sorted(telem.keys()) if telem else [],
            )
        if not force and now_s - last_telem_log_s < 5.0:
            return
        last_telem_log_s = now_s
        telem = snap.get("crsf_telemetry") or {}
        log.info(
            "CRSF RX summary: bytes=%d frames_ok=%d telem=%d bad_crc=%d sync_skip=%d "
            "buf=%d types=%s age=%s LQ=%s link_ok=%s",
            crsf_reader.bytes_fed,
            crsf_reader.frames_ok,
            crsf_reader.frames_telem,
            crsf_reader.frames_bad_crc,
            crsf_reader.sync_skips,
            crsf_reader.buffer_len,
            dict(crsf_reader.last_types),
            snap.get("crsf_telemetry_age_s"),
            telem.get("Uplink LQ", "—"),
            link_ok,
        )
        if bytes_rx_total == 0:
            if output_mode == CRSF_OUTPUT_UART:
                log.info(
                    "CRSF: 0 UART RX bytes (mode=uart baud=%d path=%s). "
                    "Confirm FC serial baud is %d, CRSF protocol on that port, "
                    "and Pi RX ← FC TX is wired. Telemetry = FC sensors, not RF LQ.",
                    baud_rate,
                    current_serial_path,
                    baud_rate,
                )
            else:
                log.info(
                    "CRSF: 0 UART RX bytes (mode=tx path=%s). "
                    "USB ELRS: set /hardware.html CRSF RX=3 TX=1. "
                    "FTDI: inverted half-duplex so module TX returns on the adapter RX.",
                    current_serial_path,
                )

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
                # Drain RX: some USB-UART adapters report in_waiting=0 incorrectly,
                # so always try one non-blocking read, then empty the queue.
                drained = False
                while True:
                    waiting = int(getattr(ser, "in_waiting", 0) or 0)
                    to_read = waiting if waiting > 0 else (0 if drained else 1)
                    if to_read <= 0:
                        break
                    chunk = ser.read(min(to_read, _RX_CHUNK))
                    if not chunk:
                        break
                    drained = True
                    bytes_rx_total += len(chunk)
                    if not first_rx_logged:
                        first_rx_logged = True
                        log.info(
                            "CRSF UART RX first bytes (%d): %s",
                            len(chunk),
                            chunk[:32].hex(" "),
                        )
                    n_frames = crsf_reader.feed(chunk)
                    if n_frames and log.isEnabledFor(logging.DEBUG):
                        log.debug(
                            "CRSF RX %d bytes → %d frame(s) last=%s telem_keys=%s",
                            len(chunk),
                            n_frames,
                            crsf_reader.last_type_name,
                            sorted(crsf_reader.telemetry.keys()),
                        )
                if drained:
                    _publish_telemetry()
                _log_telem_pulse()
            except (serial.SerialException, OSError) as e:
                log.warning(
                    "Serial I/O failed on %s (%s); closing and re-scanning.",
                    current_serial_path,
                    e,
                )
                close_serial()
                last_serial_attempt_s = time.monotonic()
                bytes_rx_total = 0
                first_rx_logged = False
                crsf_reader.reset_stats()
                last_link_ok = None
                continue
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        try:
            handshake_srv.close()
        except OSError:
            pass
        close_serial()
        sock.close()


if __name__ == "__main__":
    main()
