"""
Detect the TX-bridge SBC and expose pin / UART / I2C / camera defaults.

Boards:
- Raspberry Pi (gpiozero BCM, ``/dev/serial0``, ``/dev/i2c-1``, libcamera MIPI)
- Luckfox Pico Pro/Max (Rockchip RV1106, OpenIPC): sysfs GPIO, ``/dev/ttyS3``,
  ``/dev/i2c-3``, MIPI via Majestic RTP push

Override with ``BOX_BOARD`` (``raspberry`` / ``luckfox`` / ``openipc``).

Named ``sbc`` so it does not collide with Adafruit Blinka's ``board`` module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional, Tuple

BOARD_RASPBERRY = "raspberry"
BOARD_LUCKFOX = "luckfox"

# Luckfox Pico Pro/Max header (Pico-style numbering) → Linux GPIO.
# GPIO1_C7 = 55 (header pin 4), GPIO1_C6 = 54 (PWM10_M1, pin 5),
# GPIO1_C4 = 52. UART3_M1 is /dev/ttyS3 (GPIO1_D0/D1); I2C3_M1 is /dev/i2c-3.
_LUCKFOX_LED_GPIO = 55
_LUCKFOX_SERVO_GPIO = 54
_LUCKFOX_DRONE_GPIO = 52
_LUCKFOX_PWMCHIP = 10
_LUCKFOX_I2C_BUS = 3
_LUCKFOX_UART = "/dev/ttyS3"


@dataclass(frozen=True)
class BoardProfile:
    name: str
    display_name: str
    gpio_backend: str  # "gpiozero" | "sysfs"
    led_pin: int
    servo_pin: int
    drone_power_pin: int
    servo_pwmchip: Optional[int]
    i2c_bus: int
    uart_port: str
    uart_candidates: Tuple[str, ...]
    uart_aliases: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    camera_backend: str = "libcamera"  # "libcamera" | "openipc"
    uart_enable_hint: str = ""


_PI_PROFILE = BoardProfile(
    name=BOARD_RASPBERRY,
    display_name="Raspberry Pi",
    gpio_backend="gpiozero",
    led_pin=17,
    servo_pin=13,
    drone_power_pin=26,
    servo_pwmchip=None,
    i2c_bus=1,
    uart_port="/dev/serial0",
    uart_candidates=(
        "/dev/serial0",
        "/dev/ttyAMA0",
        "/dev/ttyS0",
        "/dev/ttyAMA10",
    ),
    uart_aliases=(
        ("uart0", "/dev/serial0"),
        ("uart", "/dev/serial0"),
        ("primary", "/dev/serial0"),
        ("serial0", "/dev/serial0"),
        ("ttyama0", "/dev/ttyAMA0"),
        ("ama0", "/dev/ttyAMA0"),
        ("ttys0", "/dev/ttyS0"),
        ("ttyama10", "/dev/ttyAMA10"),
    ),
    camera_backend="libcamera",
    uart_enable_hint=(
        "Enable serial hardware (raspi-config → Interface → Serial: login shell No, "
        "serial port Yes; on Pi 5 also dtparam=uart0 in /boot/firmware/config.txt)."
    ),
)

_LUCKFOX_PROFILE = BoardProfile(
    name=BOARD_LUCKFOX,
    display_name="Luckfox Pico Pro/Max",
    gpio_backend="sysfs",
    led_pin=_LUCKFOX_LED_GPIO,
    servo_pin=_LUCKFOX_SERVO_GPIO,
    drone_power_pin=_LUCKFOX_DRONE_GPIO,
    servo_pwmchip=_LUCKFOX_PWMCHIP,
    i2c_bus=_LUCKFOX_I2C_BUS,
    uart_port=_LUCKFOX_UART,
    uart_candidates=(
        "/dev/ttyS3",
        "/dev/ttyS4",
        "/dev/ttyS1",
        "/dev/ttyS0",
    ),
    uart_aliases=(
        ("uart", "/dev/ttyS3"),
        ("uart3", "/dev/ttyS3"),
        ("primary", "/dev/ttyS3"),
        ("serial0", "/dev/ttyS3"),
        ("ttys3", "/dev/ttyS3"),
        ("ttys4", "/dev/ttyS4"),
        ("ttys1", "/dev/ttyS1"),
        ("ttys0", "/dev/ttyS0"),
    ),
    camera_backend="openipc",
    uart_enable_hint=(
        "Enable UART3_M1 (OpenIPC pinmux / device tree) so /dev/ttyS3 exists. "
        "Do not use UART2 — that is the debug console."
    ),
)


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _device_tree_model() -> str:
    raw = _read_text("/proc/device-tree/model")
    return raw.replace("\x00", "").strip()


def _os_release_blob() -> str:
    return "\n".join(
        (
            _read_text("/etc/os-release"),
            _read_text("/usr/lib/os-release"),
        )
    ).lower()


def is_openipc() -> bool:
    """True when this process is on OpenIPC firmware (Majestic camera stack)."""
    blob = _os_release_blob()
    if "openipc" in blob or "id=openipc" in blob:
        return True
    if os.path.exists("/etc/majestic.yaml") or os.path.exists("/etc/majestic.full"):
        return True
    for path in ("/usr/bin/majestic", "/usr/sbin/majestic"):
        if os.path.exists(path):
            return True
    return False


def is_luckfox() -> bool:
    model = _device_tree_model().lower()
    if "luckfox" in model or "rv1106" in model or "rv1103" in model:
        return True
    compatible = _read_text("/proc/device-tree/compatible").lower()
    if "luckfox" in compatible or "rv1106" in compatible:
        return True
    return is_openipc()


def is_raspberry_pi() -> bool:
    model = _device_tree_model().lower()
    if "raspberry pi" in model:
        return True
    cpu = _read_text("/proc/cpuinfo").lower()
    if "raspberry pi" in cpu or "bcm27" in cpu:
        return True
    return os.path.exists("/proc/device-tree/soc/gpio@7e200000")


def detect_board() -> str:
    """Return ``raspberry`` or ``luckfox``. ``BOX_BOARD`` wins over autodetection."""
    env = (os.environ.get("BOX_BOARD") or "").strip().lower()
    if env in ("luckfox", "luckfox-pico", "luckfox_pico", "openipc", "rv1106", "rockchip"):
        return BOARD_LUCKFOX
    if env in ("pi", "raspberry", "raspberrypi", "raspberry-pi", "rpi"):
        return BOARD_RASPBERRY
    if is_luckfox() and not is_raspberry_pi():
        return BOARD_LUCKFOX
    if is_luckfox():
        return BOARD_LUCKFOX
    return BOARD_RASPBERRY


@lru_cache(maxsize=1)
def get_board_profile() -> BoardProfile:
    if detect_board() == BOARD_LUCKFOX:
        return _LUCKFOX_PROFILE
    return _PI_PROFILE


def camera_backend() -> str:
    """``libcamera`` (Pi) or ``openipc`` (Majestic). ``BOX_CAMERA_BACKEND`` overrides."""
    env = (os.environ.get("BOX_CAMERA_BACKEND") or "").strip().lower()
    if env in ("openipc", "majestic", "luckfox"):
        return "openipc"
    if env in ("libcamera", "rpicam", "pi", "raspberry"):
        return "libcamera"
    if env:
        return env
    return get_board_profile().camera_backend
