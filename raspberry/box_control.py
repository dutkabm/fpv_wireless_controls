"""
Raspberry Pi 4 ground-station / enclosure controller.

- BME280 or BMP280 on I2C: temperature (and humidity on BME280), pressure.
- ADS1115 on I2C: box battery and drone battery via 20 kΩ / 2 kΩ dividers
  (ADC sees Vbat * 2/22 → multiply measured pin voltage by 11 for Vbat).
- GPIO: status LED, servo PWM, drone power enable (e.g. MOSFET / relay gate).
- Camera stream: optional; see ``raspberry.video`` (`CameraStream`).

Wire ADS1115 A0 → box divider tap, A1 → drone divider tap; common ground.

Dataclasses live in ``raspberry.models`` (`DividerConfig`, `SystemStatus`).
"""

from __future__ import annotations

import os
import time
from typing import Literal, Optional, Tuple, Union

if __package__:
    from .models import DividerConfig, SystemStatus
    from .video import CameraStream
else:
    from models import DividerConfig, SystemStatus
    from video import CameraStream

# --- Optional env overrides (BCM pin numbers unless noted) ---
# export BOX_LED_PIN=18
# export BOX_SERVO_PIN=12
# export BOX_DRONE_POWER_PIN=16
# export BOX_I2C_BUS=1   # Linux device /dev/i2c-1 (enable I2C in raspi-config)


def _i2c_bus_id() -> int:
    return int(os.environ.get("BOX_I2C_BUS", "1"))


def _open_i2c():
    """Open the Pi hardware I2C controller by bus number (not GPIO bit-bang)."""
    bid = _i2c_bus_id()
    try:
        from adafruit_extended_bus import ExtendedI2C

        return ExtendedI2C(bid, frequency=400_000)
    except ImportError:
        import board
        import busio

        return busio.I2C(board.SCL, board.SDA, frequency=400_000)


def _probe_bme280_or_bmp280(i2c):
    """Return (sensor, kind) where kind is 'BME280' or 'BMP280'."""
    import adafruit_bme280
    import adafruit_bmp280

    for addr in (0x77, 0x76):
        try:
            dev = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
            _ = dev.temperature
            return dev, "BME280"
        except Exception:
            pass
        try:
            dev = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=addr)
            _ = dev.temperature
            return dev, "BMP280"
        except Exception:
            pass
    raise RuntimeError("No BME280 or BMP280 found on I2C (tried 0x77, 0x76).")


class EnvironmentSensor:
    """BME280 (temp + RH + pressure) or BMP280 (temp + pressure only)."""

    def __init__(self, i2c=None):
        self._i2c_owner = i2c is None
        self._i2c = i2c or _open_i2c()
        self._chip, self.kind = _probe_bme280_or_bmp280(self._i2c)

    def read(self) -> Tuple[float, Optional[float], float]:
        """
        Returns:
            temperature_c: float
            humidity_percent: float or None if BMP280
            pressure_hpa: float
        """
        t = float(self._chip.temperature)
        p = float(self._chip.pressure)
        if self.kind == "BME280":
            rh = float(self._chip.relative_humidity)
        else:
            rh = None
        return t, rh, p

    def deinit(self) -> None:
        if self._i2c_owner and self._i2c is not None:
            try:
                self._i2c.deinit()
            except Exception:
                pass
            self._i2c = None


class BatteryMonitor:
    """
    Two single-ended channels on ADS1115: box and drone battery taps.

    Default: channel 0 = box, channel 1 = drone (ADS1115 A0 / A1).
    """

    def __init__(
        self,
        i2c=None,
        ads_address: int = 0x48,
        gain: int = 1,
        divider: Optional[DividerConfig] = None,
        channel_box: int = 0,
        channel_drone: int = 1,
    ):
        from adafruit_ads1x15.ads1115 import ADS1115
        from adafruit_ads1x15.analog_in import AnalogIn

        self._divider = divider or DividerConfig()
        self._i2c_owner = i2c is None
        self._i2c = i2c or _open_i2c()
        self._ads = ADS1115(self._i2c, address=ads_address, gain=gain)
        self._ch_box = AnalogIn(self._ads, channel_box)
        self._ch_drone = AnalogIn(self._ads, channel_drone)

    def read_box_battery_v(self) -> float:
        return float(self._ch_box.voltage) * self._divider.scale_to_battery

    def read_drone_battery_v(self) -> float:
        return float(self._ch_drone.voltage) * self._divider.scale_to_battery

    def read_both_v(self) -> Tuple[float, float]:
        return self.read_box_battery_v(), self.read_drone_battery_v()

    def deinit(self) -> None:
        if self._i2c_owner and self._i2c is not None:
            try:
                self._i2c.deinit()
            except Exception:
                pass
            self._i2c = None


class BoxOutputs:
    """LED, servo, and drone power switching (gpiozero, BCM). Each output inits independently."""

    def __init__(
        self,
        led_pin: Optional[int] = None,
        servo_pin: Optional[int] = None,
        drone_power_pin: Optional[int] = None,
        drone_power_active_high: bool = False,
    ):
        self._led = None
        self._servo = None
        self._drone_power = None
        self.led_error: Optional[str] = None
        self.servo_error: Optional[str] = None
        self.drone_power_error: Optional[str] = None

        self._led_pin = int(os.environ.get("BOX_LED_PIN", led_pin if led_pin is not None else 18))
        self._servo_pin = int(
            os.environ.get("BOX_SERVO_PIN", servo_pin if servo_pin is not None else 12)
        )
        self._drone_pin = int(
            os.environ.get(
                "BOX_DRONE_POWER_PIN",
                drone_power_pin if drone_power_pin is not None else 16,
            )
        )

        try:
            from gpiozero import DigitalOutputDevice, Servo
        except Exception as e:
            msg = f"gpiozero unavailable: {e}"
            self.led_error = msg
            self.servo_error = msg
            self.drone_power_error = msg
            return

        try:
            self._led = DigitalOutputDevice(self._led_pin, initial_value=False)
        except Exception as e:
            self.led_error = str(e)

        try:
            self._servo = Servo(
                self._servo_pin,
                min_pulse_width=1.0 / 1000,
                max_pulse_width=2.0 / 1000,
            )
        except Exception as e:
            self.servo_error = str(e)

        try:
            self._drone_power = DigitalOutputDevice(
                self._drone_pin,
                active_high=drone_power_active_high,
                initial_value=not drone_power_active_high,
            )
        except Exception as e:
            self.drone_power_error = str(e)

    @property
    def led_is_on(self) -> bool:
        return bool(self._led.value) if self._led is not None else False

    @property
    def drone_power_is_on(self) -> bool:
        return bool(self._drone_power.value) if self._drone_power is not None else False

    @property
    def servo_is_active(self) -> bool:
        return self._servo is not None and self._servo.value is not None

    @property
    def servo_position(self) -> Optional[float]:
        if self._servo is None:
            return None
        v = self._servo.value
        return None if v is None else float(v)

    def _require_led(self):
        if self._led is None:
            raise RuntimeError(self.led_error or f"LED GPIO {self._led_pin} unavailable")

    def _require_servo(self):
        if self._servo is None:
            raise RuntimeError(self.servo_error or f"servo GPIO {self._servo_pin} unavailable")

    def _require_drone_power(self):
        if self._drone_power is None:
            raise RuntimeError(
                self.drone_power_error or f"drone power GPIO {self._drone_pin} unavailable"
            )

    def led_on(self) -> None:
        self._require_led()
        self._led.on()

    def led_off(self) -> None:
        self._require_led()
        self._led.off()

    def led_set(self, on: bool) -> None:
        self._require_led()
        if on:
            self._led.on()
        else:
            self._led.off()

    def servo_start(self, position: Union[float, Literal["neutral"]] = "neutral") -> None:
        """Start driving the servo (PWM on). position: -1 … 1, or 'neutral' (0)."""
        self._require_servo()
        if position == "neutral":
            self._servo.value = 0.0
        else:
            self._servo.value = max(-1.0, min(1.0, float(position)))

    def servo_stop(self) -> None:
        """Stop PWM pulses (servo floats / unloaded)."""
        self._require_servo()
        self._servo.detach()

    def drone_power_on(self) -> None:
        self._require_drone_power()
        self._drone_power.on()

    def drone_power_off(self) -> None:
        self._require_drone_power()
        self._drone_power.off()

    def drone_power_set(self, on: bool) -> None:
        self._require_drone_power()
        if on:
            self._drone_power.on()
        else:
            self._drone_power.off()

    def close(self) -> None:
        for dev in (self._led, self._servo, self._drone_power):
            if dev is None:
                continue
            try:
                dev.close()
            except Exception:
                pass


class BoxController:
    """GPIO/camera outputs; I2C sensors (env + ADC) are optional if init fails."""

    def __init__(
        self,
        led_pin: Optional[int] = None,
        servo_pin: Optional[int] = None,
        drone_power_pin: Optional[int] = None,
        drone_power_active_high: bool = True,
    ):
        self.env: Optional[EnvironmentSensor] = None
        self.batteries: Optional[BatteryMonitor] = None
        self.env_error: Optional[str] = None
        self.battery_error: Optional[str] = None
        self._i2c = None

        self.gpio = BoxOutputs(
            led_pin=led_pin,
            servo_pin=servo_pin,
            drone_power_pin=drone_power_pin,
            drone_power_active_high=drone_power_active_high,
        )
        self.camera_stream = CameraStream()

        try:
            self._i2c = _open_i2c()
        except Exception as e:
            self.env_error = f"I2C: {e}"
            self.battery_error = self.env_error
            return

        try:
            self.env = EnvironmentSensor(self._i2c)
        except Exception as e:
            self.env_error = str(e)

        try:
            self.batteries = BatteryMonitor(self._i2c)
        except Exception as e:
            self.battery_error = str(e)

    def camera_stream_start(self) -> bool:
        """Start the Pi camera network stream (env vars: see ``raspberry.video`` module docstring)."""
        return self.camera_stream.start()

    def camera_stream_stop(self) -> None:
        """Stop the Pi camera stream subprocess."""
        self.camera_stream.stop()

    def read_temperature_c(self) -> float:
        if self.env is None:
            raise RuntimeError(self.env_error or "environment sensor unavailable")
        return self.env.read()[0]

    def read_humidity_percent(self) -> Optional[float]:
        if self.env is None:
            raise RuntimeError(self.env_error or "environment sensor unavailable")
        return self.env.read()[1]

    def read_pressure_hpa(self) -> float:
        if self.env is None:
            raise RuntimeError(self.env_error or "environment sensor unavailable")
        return self.env.read()[2]

    def read_environment(self) -> Tuple[float, Optional[float], float]:
        if self.env is None:
            raise RuntimeError(self.env_error or "environment sensor unavailable")
        return self.env.read()

    def read_box_battery_v(self) -> float:
        if self.batteries is None:
            raise RuntimeError(self.battery_error or "battery monitor unavailable")
        return self.batteries.read_box_battery_v()

    def read_drone_battery_v(self) -> float:
        if self.batteries is None:
            raise RuntimeError(self.battery_error or "battery monitor unavailable")
        return self.batteries.read_drone_battery_v()

    def read_system_status(self, into: Optional[SystemStatus] = None) -> SystemStatus:
        """Fill or return a ``SystemStatus`` from current hardware."""
        st = into or SystemStatus()
        st.refresh(self)
        return st

    def close(self) -> None:
        self.camera_stream.stop()
        self.gpio.close()
        if self.batteries is not None:
            self.batteries.deinit()
        if self.env is not None:
            self.env.deinit()
        if self._i2c is not None:
            try:
                self._i2c.deinit()
            except Exception:
                pass
            self._i2c = None


# --- Standalone helpers (use after constructing shared I2C yourself, or use BoxController) ---


def read_temperature_humidity_pressure(
    env: Optional[EnvironmentSensor] = None,
) -> Tuple[float, Optional[float], float]:
    own = env is None
    e = env or EnvironmentSensor()
    try:
        return e.read()
    finally:
        if own:
            e.deinit()


def read_battery_voltages(mon: Optional[BatteryMonitor] = None) -> Tuple[float, float]:
    own = mon is None
    m = mon or BatteryMonitor()
    try:
        return m.read_both_v()
    finally:
        if own:
            m.deinit()


def box_controller_run() -> None:
    print("Box controller running (Ctrl+C to exit).")
    box = BoxController()
    status = SystemStatus()
    try:
        while True:
            status.refresh(box)
            rh_s = f"{status.humidity_percent:.1f} %" if status.humidity_percent is not None else "n/a (BMP280)"
            cam_e = status.camera_stream_error or ""
            cam_s = f"cam_on={status.camera_streaming}" + (f" err={cam_e!r}" if cam_e else "")
            print(
                f"sensor={status.sensor_kind}  T={status.temperature_c:.2f} °C  RH={rh_s}  "
                f"P={status.pressure_hpa:.1f} hPa  Vbox={status.box_battery_v:.2f} V  "
                f"Vdrone={status.drone_battery_v:.2f} V  "
                f"led={status.led_on} servo_on={status.servo_active} drone_pwr={status.drone_power_on}  "
                f"{cam_s}"
            )
            box.gpio.led_on()
            time.sleep(0.05)
            box.gpio.led_off()
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        box.close()


if __name__ == "__main__":
    box_controller_run()
