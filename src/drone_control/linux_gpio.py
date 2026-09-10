"""Linux sysfs GPIO and PWM (Luckfox / OpenIPC). Duck-types gpiozero outputs."""

from __future__ import annotations

import os
import time
from typing import Optional

_GPIO_CLASS = "/sys/class/gpio"
_PWM_CLASS = "/sys/class/pwm"

# Standard hobby-servo pulse (gpiozero Servo defaults): 1.0–2.0 ms in a 20 ms frame.
_SERVO_PERIOD_NS = 20_000_000
_SERVO_MIN_PULSE_NS = 1_000_000
_SERVO_MAX_PULSE_NS = 2_000_000


def _write(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as f:
        f.write(value)


def _read(path: str) -> str:
    with open(path, encoding="ascii") as f:
        return f.read().strip()


class SysfsDigitalOut:
    """sysfs ``/sys/class/gpio`` output with gpiozero-like ``on`` / ``off`` / ``value``."""

    def __init__(
        self,
        pin: int,
        *,
        active_high: bool = True,
        initial_value: bool = False,
    ) -> None:
        self._pin = int(pin)
        self._active_high = bool(active_high)
        self._dir = f"{_GPIO_CLASS}/gpio{self._pin}"
        self._value_path = f"{self._dir}/value"
        self._exported = False
        self._export()
        _write(f"{self._dir}/direction", "out")
        self._set_logical(bool(initial_value))

    def _export(self) -> None:
        if os.path.isdir(self._dir):
            self._exported = True
            return
        export = f"{_GPIO_CLASS}/export"
        if not os.path.exists(export):
            raise FileNotFoundError(
                f"GPIO sysfs missing ({export}); cannot drive pin {self._pin}"
            )
        try:
            _write(export, str(self._pin))
        except OSError as e:
            if not os.path.isdir(self._dir):
                raise RuntimeError(f"GPIO {self._pin} export failed: {e}") from e
        for _ in range(20):
            if os.path.exists(self._value_path):
                self._exported = True
                return
            time.sleep(0.02)
        raise RuntimeError(f"GPIO {self._pin} exported but {self._value_path} missing")

    def _set_logical(self, on: bool) -> None:
        physical = on if self._active_high else (not on)
        _write(self._value_path, "1" if physical else "0")

    @property
    def value(self) -> bool:
        raw = _read(self._value_path) == "1"
        return raw if self._active_high else (not raw)

    def on(self) -> None:
        self._set_logical(True)

    def off(self) -> None:
        self._set_logical(False)

    def close(self) -> None:
        if not self._exported:
            return
        try:
            self.off()
        except Exception:
            pass
        unexport = f"{_GPIO_CLASS}/unexport"
        try:
            if os.path.exists(unexport) and os.path.isdir(self._dir):
                _write(unexport, str(self._pin))
        except OSError:
            pass
        self._exported = False


class SysfsPwmServo:
    """Hardware PWM servo via ``/sys/class/pwm/pwmchipN`` (50 Hz, 1–2 ms pulse)."""

    def __init__(self, chip: int, channel: int = 0) -> None:
        self._chip = int(chip)
        self._channel = int(channel)
        self._chip_dir = f"{_PWM_CLASS}/pwmchip{self._chip}"
        self._pwm_dir = f"{self._chip_dir}/pwm{self._channel}"
        self._position: Optional[float] = None
        if not os.path.isdir(self._chip_dir):
            raise FileNotFoundError(
                f"{self._chip_dir} not found. Enable PWM{self._chip} "
                f"(OpenIPC pinmux / luckfox-config PWM{self._chip}_M1) for the servo."
            )
        self._export()
        _write(f"{self._pwm_dir}/period", str(_SERVO_PERIOD_NS))
        pol = f"{self._pwm_dir}/polarity"
        if os.path.exists(pol):
            try:
                _write(pol, "normal")
            except OSError:
                pass
        self.detach()

    def _export(self) -> None:
        if os.path.isdir(self._pwm_dir):
            return
        try:
            _write(f"{self._chip_dir}/export", str(self._channel))
        except OSError as e:
            if not os.path.isdir(self._pwm_dir):
                raise RuntimeError(
                    f"PWM chip {self._chip} channel {self._channel} export failed: {e}"
                ) from e
        for _ in range(20):
            if os.path.exists(f"{self._pwm_dir}/duty_cycle"):
                return
            time.sleep(0.02)
        raise RuntimeError(f"PWM {self._pwm_dir} did not appear after export")

    def _pulse_ns(self, position: float) -> int:
        p = max(-1.0, min(1.0, float(position)))
        span = _SERVO_MAX_PULSE_NS - _SERVO_MIN_PULSE_NS
        return int(_SERVO_MIN_PULSE_NS + (p + 1.0) * 0.5 * span)

    def _enable(self, on: bool) -> None:
        _write(f"{self._pwm_dir}/enable", "1" if on else "0")

    @property
    def value(self) -> Optional[float]:
        return self._position

    @value.setter
    def value(self, raw: Optional[float]) -> None:
        if raw is None:
            self.detach()
            return
        self._apply(float(raw))

    def _apply(self, position: float) -> None:
        pulse = self._pulse_ns(position)
        _write(f"{self._pwm_dir}/duty_cycle", str(pulse))
        self._enable(True)
        self._position = max(-1.0, min(1.0, float(position)))

    def detach(self) -> None:
        try:
            self._enable(False)
        except OSError:
            pass
        self._position = None

    def close(self) -> None:
        self.detach()
        unexport = f"{self._chip_dir}/unexport"
        try:
            if os.path.exists(unexport) and os.path.isdir(self._pwm_dir):
                _write(unexport, str(self._channel))
        except OSError:
            pass
