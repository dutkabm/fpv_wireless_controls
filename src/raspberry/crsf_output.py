"""
CRSF serial output mode for the Pi bridge (chosen at process start).

``tx`` — USB-UART to an ELRS/Crossfire TX module (wireless), baud ``CRSF_BAUD_TX``.
``uart`` — Pi SoC UART emulates an ELRS RX wired to the flight controller
(CRSF RC out + telemetry in), baud ``CRSF_BAUD_UART``.
"""

from __future__ import annotations

import os
from typing import Optional

CRSF_OUTPUT_TX = "tx"
CRSF_OUTPUT_UART = "uart"
CRSF_OUTPUT_MODES = (CRSF_OUTPUT_TX, CRSF_OUTPUT_UART)

# Fixed CRSF serial baud by output mode (not configurable via controller_map.txt).
CRSF_BAUD_UART = 115200  # Pi-as-RX ↔ flight controller
CRSF_BAUD_TX = 400000  # USB ELRS / Crossfire TX module

# Raspberry Pi primary UART (GPIO 14/15). Prefer the stable symlink; it points at
# ttyAMA0 / ttyS0 / ttyAMA10 depending on model and config.
DEFAULT_UART_PORT = "/dev/serial0"

# Tried in order when the preferred node is missing (ENOENT).
UART_PORT_CANDIDATES = (
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    "/dev/ttyAMA10",
)

_UART_PORT_ALIASES = {
    "uart0": DEFAULT_UART_PORT,
    "uart": DEFAULT_UART_PORT,
    "primary": DEFAULT_UART_PORT,
    "serial0": "/dev/serial0",
    "ttyama0": "/dev/ttyAMA0",
    "ama0": "/dev/ttyAMA0",
    "ttys0": "/dev/ttyS0",
    "ttyama10": "/dev/ttyAMA10",
}


def normalize_crsf_output_mode(value: Optional[str], *, default: str = CRSF_OUTPUT_UART) -> str:
    s = (value or "").strip().lower()
    aliases = {
        "tx": CRSF_OUTPUT_TX,
        "module": CRSF_OUTPUT_TX,
        "tx-module": CRSF_OUTPUT_TX,
        "tx_module": CRSF_OUTPUT_TX,
        "usb": CRSF_OUTPUT_TX,
        "uart": CRSF_OUTPUT_UART,
        "direct": CRSF_OUTPUT_UART,
        "fc": CRSF_OUTPUT_UART,
        "drone": CRSF_OUTPUT_UART,
        "ttyama": CRSF_OUTPUT_UART,
    }
    if s in aliases:
        return aliases[s]
    if s in CRSF_OUTPUT_MODES:
        return s
    return default


def baud_for_crsf_output(mode: str) -> int:
    """Return the fixed baud for ``tx`` or ``uart`` output mode."""
    if normalize_crsf_output_mode(mode) == CRSF_OUTPUT_TX:
        return CRSF_BAUD_TX
    return CRSF_BAUD_UART


def resolve_uart_port(value: Optional[str], *, default: str = DEFAULT_UART_PORT) -> str:
    """Map config/CLI UART names (e.g. ``uart0``) to a device path preference."""
    s = (value or "").strip()
    if not s:
        return default
    key = s.lower()
    if key in _UART_PORT_ALIASES:
        return _UART_PORT_ALIASES[key]
    if key.startswith("tty") and "/" not in s:
        return f"/dev/{s}"
    return s


def pick_uart_device(preferred: Optional[str] = None) -> Optional[str]:
    """
    Return the first existing UART device path.

    Tries ``preferred`` first, then :data:`UART_PORT_CANDIDATES`.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    pref = resolve_uart_port(preferred, default=DEFAULT_UART_PORT)
    for path in (pref, *UART_PORT_CANDIDATES):
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    for path in ordered:
        if os.path.exists(path):
            return path
    return None
