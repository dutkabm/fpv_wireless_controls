"""
Data models for the Raspberry Pi box controller (divider config, live status).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


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

    ``box`` must be a :class:`raspberry.box_control.BoxController` instance.
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
    camera_stream_error: Optional[str] = None
    env_error: Optional[str] = None
    battery_error: Optional[str] = None

    def refresh(self, box) -> None:
        """Pull sensors, ADC, output pin state, and camera stream state from a live box controller."""
        self.monotonic_s = time.monotonic()
        self.env_error = getattr(box, "env_error", None)
        self.battery_error = getattr(box, "battery_error", None)
        if box.env is not None:
            self.sensor_kind = box.env.kind
            t, rh, p = box.read_environment()
            self.temperature_c = t
            self.humidity_percent = rh
            self.pressure_hpa = p
        else:
            self.sensor_kind = ""
        if box.batteries is not None:
            self.box_battery_v, self.drone_battery_v = box.batteries.read_both_v()
        self.led_on = box.gpio.led_is_on
        self.servo_active = box.gpio.servo_is_active
        self.servo_position = box.gpio.servo_position
        self.drone_power_on = box.gpio.drone_power_is_on
        cs = getattr(box, "camera_stream", None)
        if cs is not None:
            self.camera_streaming = cs.is_running
            self.camera_stream_error = cs.last_error
        else:
            self.camera_streaming = False
            self.camera_stream_error = None

    @classmethod
    def capture(cls, box) -> SystemStatus:
        s = cls()
        s.refresh(box)
        return s
