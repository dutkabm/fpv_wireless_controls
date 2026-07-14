"""
Shared CRSF serial + telemetry snapshot for the Pi bridge process.

``raspberry.main`` writes; ``raspberry.box_server`` reads for ``GET /api/status``.
Standalone ``box_server`` leaves defaults (serial closed, empty telemetry).

In ``uart`` mode the Pi emulates an ELRS RX talking to the FC: expect battery /
GPS / attitude telemetry, not RF LINK_STATISTICS (those come from a real radio).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_TELEM_STALE_S = 2.0
_BUS_STALE_S = 30.0  # DEVICE_PING can be rare; keep bus_ok for UI after last frame

_FC_KEYS = frozenset(
    {
        "Voltage",
        "Current",
        "Capacity",
        "Remaining",
        "Latitude",
        "Longitude",
        "Altitude",
        "Speed",
        "Heading",
        "Pitch",
        "Roll",
        "Yaw",
        "Flight Mode",
    }
)

_lock = threading.Lock()
_output_mode: str = ""
_serial_open: bool = False
_serial_path: Optional[str] = None
_telemetry: Dict[str, Any] = {}
_telemetry_mono: float = 0.0
_link_mono: float = 0.0
_fc_mono: float = 0.0
_bus_mono: float = 0.0


def set_output_mode(mode: str) -> None:
    global _output_mode
    with _lock:
        _output_mode = (mode or "").strip()


def set_serial(*, open_: bool, path: Optional[str] = None) -> None:
    global _serial_open, _serial_path
    with _lock:
        _serial_open = bool(open_)
        _serial_path = path if open_ and path else None
        if not open_:
            _serial_path = None


def note_bus_rx() -> None:
    """Mark that a valid CRSF frame was received (ping/info/channels/telem)."""
    global _bus_mono
    with _lock:
        _bus_mono = time.monotonic()


def update_telemetry(fields: Dict[str, Any]) -> None:
    """Replace telemetry snapshot (caller passes a copy of the parser dict)."""
    global _telemetry, _telemetry_mono, _link_mono, _fc_mono
    now = time.monotonic()
    with _lock:
        _telemetry = dict(fields)
        _telemetry_mono = now
        if "Uplink LQ" in fields:
            _link_mono = now
        if any(k in _FC_KEYS for k in fields):
            _fc_mono = now


def snapshot() -> Dict[str, Any]:
    """JSON-friendly CRSF fields for the status API."""
    now = time.monotonic()
    with _lock:
        telem = dict(_telemetry)
        age = (now - _telemetry_mono) if _telemetry_mono > 0 else None
        link_age = (now - _link_mono) if _link_mono > 0 else None
        fc_age = (now - _fc_mono) if _fc_mono > 0 else None
        bus_age = (now - _bus_mono) if _bus_mono > 0 else None
        serial_open = _serial_open
        serial_path = _serial_path
        mode = _output_mode

    lq = telem.get("Uplink LQ")
    try:
        lq_n = int(lq) if lq is not None else 0
    except (TypeError, ValueError):
        lq_n = 0

    rf_link_ok = (
        serial_open
        and lq_n > 0
        and link_age is not None
        and link_age <= _TELEM_STALE_S
    )

    has_fc_fields = any(k in telem for k in _FC_KEYS)
    fc_ok = (
        serial_open
        and has_fc_fields
        and fc_age is not None
        and fc_age <= _TELEM_STALE_S
    )

    bus_ok = serial_open and bus_age is not None and bus_age <= _BUS_STALE_S

    # UI connected: RF link, FC sensors, or recent CRSF bus traffic (e.g. DEVICE_PING).
    connected = rf_link_ok or fc_ok or bus_ok

    return {
        "crsf_serial_open": serial_open,
        "crsf_serial_path": serial_path or "",
        "crsf_output": mode,
        "crsf_link_ok": connected,
        "crsf_rf_link_ok": rf_link_ok,
        "crsf_fc_ok": fc_ok,
        "crsf_bus_ok": bus_ok,
        "crsf_bus_age_s": None if bus_age is None else round(bus_age, 3),
        "crsf_telemetry": telem,
        "crsf_telemetry_age_s": None if age is None else round(age, 3),
    }
