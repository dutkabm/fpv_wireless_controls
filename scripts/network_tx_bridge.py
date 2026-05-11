#!/usr/bin/env python3
"""
Raspberry Pi (or any Linux host): receive joystick channel frames over UDP, forward CRSF
to the transmitter module over serial — same CRSF framing as minirex_headless.py.

Run near the scripts directory so controller_map serial settings are optional for autodetect;
serial port defaults follow General.serial_port when a config file exists.
"""

from __future__ import annotations

import argparse
import configparser
import os
import socket
import threading
import time
from enum import IntEnum
from typing import List, Optional

import serial

from network_rc_protocol import (
    CHANNEL_PACKET_MAGIC,
    CHANNEL_PAYLOAD_LEN,
    DEFAULT_HANDSHAKE_TCP_PORT,
    DEFAULT_UDP_CHANNEL_PORT,
    HANDSHAKE_OK_LINE,
    unpack_channel_datagram,
)
from serial_autodetect import autodetect_serial_port, is_autoselect_serial_port

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PRIMARY_MAP = os.path.join(_SCRIPT_DIR, "controller_map.txt")
_FALLBACK_MAP = os.path.join(_SCRIPT_DIR, "controler_map.txt")
_DEFAULT_BRIDGE_CONFIG = (
    _PRIMARY_MAP
    if os.path.exists(_PRIMARY_MAP)
    else (_FALLBACK_MAP if os.path.exists(_FALLBACK_MAP) else _PRIMARY_MAP)
)

CRSF_SYNC_BYTE = 0xC8


class CRSFPacketType(IntEnum):
    RC_CHANNELS_PACKED = 0x16


def crc8_dvb_s2(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0xD5) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def packCrsfToBytes(channels):
    if len(channels) != 16:
        raise ValueError("CRSF must have 16 channels")
    result = bytearray()
    bit_buffer = 0
    bits_in_buffer = 0
    for ch in channels:
        bit_buffer |= ch << bits_in_buffer
        bits_in_buffer += 11
        while bits_in_buffer >= 8:
            result.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bits_in_buffer -= 8
    if bits_in_buffer > 0:
        result.append(bit_buffer & 0xFF)
    return bytes(result)


def channelsCrsfToChannelsPacket(channels):
    payload = bytearray([CRSFPacketType.RC_CHANNELS_PACKED])
    payload += packCrsfToBytes(channels)
    length = len(payload) + 1
    packet = bytearray([CRSF_SYNC_BYTE, length]) + payload
    crc = crc8_dvb_s2(packet[2:])
    packet.append(crc)
    return packet


def map_to_crsf(value):
    return int((value - 1000) * 2047 / 1000)


def pwm_channels_to_crsf_packet(channels_1000_2000: List[int]) -> bytes:
    capped = [max(1000, min(2000, int(c))) for c in channels_1000_2000]
    crsfs = [map_to_crsf(c) for c in capped]
    return bytes(channelsCrsfToChannelsPacket(crsfs))


def _strip_inline_comment(value: Optional[str]) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def load_serial_from_config(config_path: str) -> tuple[str, int]:
    default_port = "AUTO"
    default_baud = 921600
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


def main():
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
    ap.add_argument("--baud", type=int, default=None, help="Baud rate (default from controller_map.txt or 921600)")
    ap.add_argument(
        "--config",
        default=_DEFAULT_BRIDGE_CONFIG,
        help="INI file path for baud/serial hints (Mini Rex controller_map)",
    )
    ap.add_argument("--hz", type=float, default=50.0, help="CRSF transmit rate toward TX")
    ap.add_argument("--failsafe-ms", type=float, default=500.0, help="Hold last channels; fail-safe defaults after this latency")
    args = ap.parse_args()

    cfg_serial, cfg_baud = load_serial_from_config(args.config)
    serial_port = args.serial if args.serial is not None else cfg_serial
    baud_rate = args.baud if args.baud is not None else cfg_baud

    prev_serial = serial_port
    resolved = autodetect_serial_port(baud_rate, serial_port)
    if resolved is None:
        raise SystemExit("No usable serial port found; connect the TX USB-UART or set --serial explicitly.")
    if is_autoselect_serial_port(prev_serial):
        print(f"Using auto-detected serial port: {resolved}")
    elif str(prev_serial).strip() != str(resolved).strip():
        print(f"Configured port {prev_serial!r} missing; using {resolved!r}.")
    serial_port = resolved

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
                conn, _addr = handshake_srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            try:
                conn.settimeout(5.0)
                data = conn.recv(64)
                if data and data.strip() == CHANNEL_PACKET_MAGIC:
                    conn.sendall(HANDSHAKE_OK_LINE)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    hs = threading.Thread(target=handshake_loop, daemon=True)
    hs.start()

    ser = serial.Serial(serial_port, baud_rate, timeout=0)
    print(
        f"Serial open: {serial_port} @ {baud_rate}. "
        f"UDP {args.bind}:{args.port} · TCP handshake {args.bind}:{args.handshake_port} ({args.hz:.0f} Hz CRSF)"
    )

    period = 1.0 / max(args.hz, 1.0)
    fail_ns = int(max(args.failsafe_ms, 0.0) * 1e9)

    lock = threading.Lock()
    latest: Optional[List[int]] = None
    last_rx = 0

    def recv_loop():
        nonlocal latest, last_rx
        while True:
            try:
                data, _addr = sock.recvfrom(CHANNEL_PAYLOAD_LEN)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            except OSError:
                time.sleep(0.01)
                continue
            parsed = unpack_channel_datagram(data)
            if parsed is None:
                continue
            with lock:
                latest = parsed
                last_rx = time.monotonic_ns()

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    failsafe_pwm = [1500] * 16
    try:
        while True:
            now = time.monotonic_ns()
            with lock:
                ch = list(latest) if latest is not None else None
                stale = (now - last_rx) > fail_ns if fail_ns > 0 else False
            if ch is None or stale:
                ch = failsafe_pwm
            ser.write(pwm_channels_to_crsf_packet(ch))
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        try:
            handshake_srv.close()
        except OSError:
            pass
        ser.close()
        sock.close()


if __name__ == "__main__":
    main()
