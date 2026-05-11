"""
Auto-detect the USB serial port used for CRSF output to an ELRS / Crossfire TX.

Scans available COM/tty devices, briefly listens for CRSF frames (sync 0xC8), then
falls back to USB-UART bridge heuristics (STM32 VCP, CP210x, CH340, FTDI, etc.).
"""
from __future__ import annotations

import time

import serial
import serial.tools.list_ports

CRSF_SYNC_BYTE = 0xC8

_PREFERRED_HINTS = (
    "stm32",
    "virtual com",
    "ch340",
    "ch341",
    "cp210",
    "silicon labs",
    "ftdi",
    "usb serial",
    "usb-serial",
    "acm",
    "elrs",
    "expresslrs",
    "radiomaster",
    "happymodel",
    "betafpv",
    "crsf",
    "crossfire",
)

_DEPRIORITIZE_HINTS = (
    "bluetooth",
    "debug",
    "modem",
    "gps",
    "braille",
    "camera",
)


def _score_port(description: str | None, hwid: str | None) -> int:
    blob = f"{description or ''} {hwid or ''}".lower()
    score = 0
    for hint in _PREFERRED_HINTS:
        if hint in blob:
            score += 3
    for hint in _DEPRIORITIZE_HINTS:
        if hint in blob:
            score -= 5
    return score


def _sorted_usb_serial_ports():
    ports = list(serial.tools.list_ports.comports())
    ports.sort(key=lambda p: (-_score_port(p.description, p.hwid), p.device))
    return ports


def _looks_like_crsf_traffic(buf: bytearray) -> bool:
    # Minimal check: extender address CRSF_SYNC and plausible frame length field
    for i in range(len(buf) - 2):
        if buf[i] != CRSF_SYNC_BYTE:
            continue
        frame_len = buf[i + 1]
        if 2 <= frame_len <= 62:
            return True
    return False


def _probe_port_for_crsf(device: str, baud_rate: int, listen_s: float = 0.22) -> bool:
    try:
        ser = serial.Serial(device, baud_rate, timeout=0.02)
    except (serial.SerialException, OSError):
        return False
    try:
        acc = bytearray()
        deadline = time.monotonic() + listen_s
        while time.monotonic() < deadline:
            waiting = ser.in_waiting
            if waiting:
                chunk = ser.read(min(waiting, 256))
                acc.extend(chunk)
                if len(acc) > 1024:
                    del acc[:-512]
                if _looks_like_crsf_traffic(acc):
                    return True
            else:
                time.sleep(0.008)
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass


def is_autoselect_serial_port(serial_port: str | None) -> bool:
    if serial_port is None:
        return True
    s = str(serial_port).strip().lower()
    return s in ("", "not connected", "auto", "detect", "none")


def autodetect_serial_port(baud_rate: int, configured_port: str | None = None) -> str | None:
    """
    Return the device path (e.g. COM3, /dev/ttyACM0) to use, or None if none exists.

    If configured_port is set and not an auto placeholder, returns it unchanged
    when it still appears in the current port list (user explicitly chose it).
    """
    ports = _sorted_usb_serial_ports()
    devices = [p.device for p in ports]

    if not is_autoselect_serial_port(configured_port):
        chosen = str(configured_port).strip()
        if chosen in devices:
            return chosen

    if not devices:
        return None

    for p in ports:
        if _probe_port_for_crsf(p.device, baud_rate):
            return p.device

    return ports[0].device
