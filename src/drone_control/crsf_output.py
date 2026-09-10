"""
CRSF serial output mode for the TX bridge (chosen at process start).

``tx`` — USB-UART to an ELRS/Crossfire TX module (wireless), baud ``CRSF_BAUD_TX``.
``uart`` — SoC UART emulates an ELRS RX wired to the flight controller
(CRSF RC out + telemetry in), baud ``CRSF_BAUD_UART``.

UART device defaults follow the board profile (Pi ``/dev/serial0``, Luckfox ``/dev/ttyS3``).
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from .sbc import get_board_profile
except ImportError:
    from sbc import get_board_profile  # type: ignore

CRSF_OUTPUT_TX = "tx"
CRSF_OUTPUT_UART = "uart"
CRSF_OUTPUT_MODES = (CRSF_OUTPUT_TX, CRSF_OUTPUT_UART)

# Fixed CRSF serial baud by output mode (not configurable via controller_map.txt).
CRSF_BAUD_UART = 420000  # board-as-RX ↔ FC (Betaflight/ELRS CRSF)
CRSF_BAUD_TX = 400000  # USB ELRS / Crossfire TX module


def _profile():
    return get_board_profile()


DEFAULT_UART_PORT = _profile().uart_port
UART_PORT_CANDIDATES = _profile().uart_candidates


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


def resolve_uart_port(value: Optional[str], *, default: Optional[str] = None) -> str:
    """Map config/CLI UART names (e.g. ``uart0``, ``uart3``) to a device path preference."""
    prof = _profile()
    aliases = dict(prof.uart_aliases)
    fallback = default if default else prof.uart_port
    s = (value or "").strip()
    if not s:
        return fallback
    key = s.lower()
    if key in aliases:
        return aliases[key]
    if key.startswith("tty") and "/" not in s:
        return f"/dev/{s}"
    return s


def pick_uart_device(preferred: Optional[str] = None) -> Optional[str]:
    """
    Return the first existing UART device path.

    Tries ``preferred`` first, then the board UART candidates (Pi serial0 / Luckfox ttyS3).
    """
    prof = _profile()
    seen: set[str] = set()
    ordered: list[str] = []
    pref = resolve_uart_port(preferred, default=prof.uart_port)
    for path in (pref, *prof.uart_candidates):
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    for path in ordered:
        if os.path.exists(path):
            return path
    return None
