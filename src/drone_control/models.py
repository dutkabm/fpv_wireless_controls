"""
Data models for the Raspberry Pi box controller (divider config, live status).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class DividerConfig:
    """Resistor divider from battery+ to ADC (top R to batt, bottom R to GND)."""

    r_top_ohms: float = 20_000.0
    r_bottom_ohms: float = 2_000.0

    @property
    def scale_to_battery(self) -> float:
        """Multiply ADC pin voltage by this to get battery voltage."""
        return (self.r_top_ohms + self.r_bottom_ohms) / self.r_bottom_ohms


@dataclass
class SystemStatus:
    """
    Latest enclosure / power / GPIO snapshot. Call ``refresh(box)`` on one
    instance over time to accumulate current state, or ``capture(box)`` for a
    one-off read.

    ``box`` must be a :class:`drone_control.box_control.BoxController` instance.
    """

    monotonic_s: float = 0.0
    sensor_kind: str = ""
    temperature_c: float = 0.0
    humidity_percent: Optional[float] = None
    pressure_hpa: float = 0.0
    box_battery_v: float = 0.0
    drone_battery_v: float = 0.0
    led_on: bool = False
    servo_active: bool = False
    servo_position: Optional[float] = None
    drone_power_on: bool = False
    camera_streaming: bool = False
    camera_source: str = "mipi"
    camera_stream_error: Optional[str] = None
    stream: Optional[dict[str, Any]] = None
    stream_url: Optional[str] = None
    env_error: Optional[str] = None
    battery_error: Optional[str] = None
    box_io_enabled: bool = True

    def refresh(self, box) -> None:
        """Pull sensors, ADC, output pin state, and camera stream state from a live box controller."""
        self.monotonic_s = time.monotonic()
        self.box_io_enabled = bool(getattr(box, "enclosure_io", True))
        self.env_error = getattr(box, "env_error", None)
        self.battery_error = getattr(box, "battery_error", None)
        if box.env is not None:
            self.sensor_kind = box.env.kind
            try:
                t, rh, p = box.read_environment()
                self.temperature_c = t
                self.humidity_percent = rh
                self.pressure_hpa = p
            except (OSError, RuntimeError) as e:
                if isinstance(e, OSError):
                    box.mark_sensor_failure("environment", e)
                self.env_error = getattr(box, "env_error", None) or str(e)
                self.sensor_kind = ""
        else:
            self.sensor_kind = ""
        if box.batteries is not None:
            try:
                self.box_battery_v, self.drone_battery_v = box.batteries.read_both_v()
            except OSError as e:
                box.mark_sensor_failure("ADC", e)
                self.battery_error = getattr(box, "battery_error", None) or str(e)
        self.led_on = box.gpio.led_is_on
        self.servo_active = box.gpio.servo_is_active
        self.servo_position = box.gpio.servo_position
        self.drone_power_on = box.gpio.drone_power_is_on
        cs = getattr(box, "camera_stream", None)
        if cs is not None:
            self.camera_streaming = cs.is_running
            self.camera_source = getattr(cs, "source", None) or "mipi"
            self.camera_stream_error = cs.last_error
        else:
            self.camera_streaming = False
            self.camera_source = "mipi"
            self.camera_stream_error = None
        self.stream = _stream_for_box(box)
        url = None
        if self.stream:
            url = self.stream.get("url")
            src = self.stream.get("source")
            if src:
                self.camera_source = str(src)
        self.stream_url = url

    @classmethod
    def capture(cls, box) -> SystemStatus:
        s = cls()
        s.refresh(box)
        return s


def _stream_for_box(box) -> Optional[dict[str, Any]]:
    """Live playable stream: OpenIPC encoder process, else Pi RTP if Video is on."""
    try:
        from drone_control.stream_switch import current_stream_info

        info = current_stream_info()
        if info:
            return info
    except Exception:
        pass
    cs = getattr(box, "camera_stream", None)
    if cs is None or not getattr(cs, "is_running", False):
        return None
    try:
        from common.stream_info import rtp_stream
    except ImportError:
        return None
    return rtp_stream(getattr(cs, "source", None) or "mipi").to_dict()
