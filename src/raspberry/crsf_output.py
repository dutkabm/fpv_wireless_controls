"""
CRSF serial output mode for the Pi bridge (chosen at process start).

``tx`` — USB-UART to an ELRS/Crossfire TX module (wireless).
``uart`` — Pi SoC UART wired directly to the flight controller CRSF RX.
"""

from __future__ import annotations

from typing import Optional

CRSF_OUTPUT_TX = "tx"
CRSF_OUTPUT_UART = "uart"
CRSF_OUTPUT_MODES = (CRSF_OUTPUT_TX, CRSF_OUTPUT_UART)

# Raspberry Pi UART0 (PL011) on GPIO 14/15 — typically /dev/ttyAMA0.
DEFAULT_UART_PORT = "/dev/ttyAMA0"

_UART_PORT_ALIASES = {
    "uart0": DEFAULT_UART_PORT,
    "ttyama0": DEFAULT_UART_PORT,
    "ama0": DEFAULT_UART_PORT,
    "serial0": "/dev/serial0",
    "ttyama": DEFAULT_UART_PORT,
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


def resolve_uart_port(value: Optional[str], *, default: str = DEFAULT_UART_PORT) -> str:
    """Map config/CLI UART names (e.g. ``uart0``) to a device path."""
    s = (value or "").strip()
    if not s:
        return default
    key = s.lower()
    if key in _UART_PORT_ALIASES:
        return _UART_PORT_ALIASES[key]
    # Bare tty name without /dev/
    if key.startswith("tty") and "/" not in s:
        return f"/dev/{s}"
    return s
