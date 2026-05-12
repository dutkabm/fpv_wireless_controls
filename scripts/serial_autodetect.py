"""
Auto-detect the USB serial port used for CRSF output to an ELRS / Crossfire TX.

Scans available COM/tty devices, briefly listens for CRSF frames (sync 0xC8), then
falls back to USB-UART bridge heuristics (STM32 VCP, CP210x, CH340, FTDI, etc.).
"""
from __future__ import annotations

import re
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

# Linux exposes legacy 16550-style motherboard UARTs as /dev/ttyS*, ARM SoC UARTs as
# /dev/ttyAMA*/ttySAC*, etc. Modern ELRS / Crossfire TX modules connect via USB-UART
# bridges and surface as /dev/ttyUSB* or /dev/ttyACM*. The legacy nodes often exist
# even when no hardware is wired to them; opening one then fails with EIO at
# tcgetattr time. Exclude them from autodetect candidates so we don't fall back
# to a phantom port. Users who really want one can pass --serial explicitly.
_LEGACY_DEVICE_RE = re.compile(r"^/dev/tty(S|AMA|SAC|MFD|PS|O)\d+$")


def _is_legacy_linux_uart(device: str) -> bool:
    return bool(_LEGACY_DEVICE_RE.match(device or ""))


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


def _sorted_usb_serial_ports(*, include_legacy: bool = False):
    ports = list(serial.tools.list_ports.comports())
    if not include_legacy:
        ports = [p for p in ports if not _is_legacy_linux_uart(p.device)]
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


def _probe_port_for_crsf(device: str, baud_rate: int, listen_s: float = 0.22) -> tuple[bool, bool]:
    """Return (openable, saw_crsf). `openable=False` means the device can't be opened
    (e.g. legacy ttyS* with no real UART → EIO at tcgetattr) and should never be
    used as a fallback choice."""
    try:
        ser = serial.Serial(device, baud_rate, timeout=0.02)
    except (serial.SerialException, OSError):
        return False, False
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
                    return True, True
            else:
                time.sleep(0.008)
        return True, False
    except (serial.SerialException, OSError):
        return False, False
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
    when it still appears in the current port list (user explicitly chose it),
    even if it is a legacy UART node — the user picked it deliberately.
    """
    # Honor an explicit user choice (including legacy /dev/ttyS* if requested).
    if not is_autoselect_serial_port(configured_port):
        chosen = str(configured_port).strip()
        all_devices = [p.device for p in _sorted_usb_serial_ports(include_legacy=True)]
        if chosen in all_devices:
            return chosen

    ports = _sorted_usb_serial_ports()
    if not ports:
        return None

    openable_fallback: str | None = None
    for p in ports:
        openable, saw_crsf = _probe_port_for_crsf(p.device, baud_rate)
        if saw_crsf:
            return p.device
        if openable and openable_fallback is None:
            openable_fallback = p.device

    return openable_fallback
