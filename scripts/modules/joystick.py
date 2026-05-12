"""Pygame joystick discovery, controller_map parsing, mapping maintenance, and PWM channel sampling."""

from __future__ import annotations

import configparser
import logging
import os
import threading
from typing import Dict, List, Optional, Tuple

import pygame

_LOG = logging.getLogger(__name__)


def _strip_inline_comment(value: Optional[str]) -> str:
    """ConfigParser does not strip inline '# ...' comments; parse after removing them."""
    if value is None:
        return ""
    s = str(value).strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def map_axis(value: float) -> int:
    return int(1500 + value * 500)


def map_button(value: object) -> int:
    return 2000 if value else 1000


def get_pwm_channels_from_joystick(
    joystick: Optional[pygame.joystick.Joystick],
    axis_channel_map: Dict,
    button_channel_map: Dict,
    hat_channel_map: Dict,
) -> List[int]:
    if threading.current_thread() is threading.main_thread():
        pygame.event.pump()

    channels = [1500] * 16
    if not joystick:
        return channels

    axis_values = {}
    for i in range(joystick.get_numaxes()):
        axis_values[f"axis_{i}"] = joystick.get_axis(i)

    for axis_key, mapping in axis_channel_map.items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = map_axis(axis_values.get(axis_key, 0.0))
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    button_values = {}
    for i in range(joystick.get_numbuttons()):
        button_values[f"button_{i}"] = joystick.get_button(i)

    for button_key, mapping in button_channel_map.items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = map_button(button_values.get(button_key, 0))
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    hat_values = {}
    for i in range(joystick.get_numhats()):
        hat = joystick.get_hat(i)
        hat_values[f"hat_{i}_x"] = hat[0]
        hat_values[f"hat_{i}_y"] = hat[1]

    for hat_key_x, mapping in hat_channel_map["x"].items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = 1500 + hat_values.get(hat_key_x, 0) * 500
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    for hat_key_y, mapping in hat_channel_map["y"].items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = 1500 + hat_values.get(hat_key_y, 0) * 500
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    for i in range(16):
        channels[i] = max(1000, min(2000, channels[i]))
    return channels


def load_controller_config(config_path: str) -> Tuple[Optional[int], Dict, Dict, Dict]:
    default_joystick_index: Optional[int] = None
    axis_map: Dict = {}
    button_map: Dict = {}
    hat_map = {"x": {}, "y": {}}

    if not os.path.exists(config_path):
        return default_joystick_index, axis_map, button_map, hat_map

    config = configparser.ConfigParser()
    config.read(config_path)

    if "General" in config:
        general = config["General"]
        _ji = _strip_inline_comment(general.get("joystick_index", fallback="-1")).strip().lower()
        if _ji == "auto":
            pygame.joystick.init()
            default_joystick_index = 0 if pygame.joystick.get_count() > 0 else None
        else:
            try:
                ji = int(_ji)
            except ValueError:
                ji = -1
            default_joystick_index = None if ji < 0 else ji

    if "AxisMappings" in config:
        for axis_key in config["AxisMappings"]:
            mapping_str = _strip_inline_comment(config["AxisMappings"][axis_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                axis_map[axis_key] = mapping

    if "ButtonMappings" in config:
        for button_key in config["ButtonMappings"]:
            mapping_str = _strip_inline_comment(config["ButtonMappings"][button_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                button_map[button_key] = mapping

    if "HatMappings" in config:
        for hat_key in config["HatMappings"]:
            mapping_str = _strip_inline_comment(config["HatMappings"][hat_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                if hat_key.endswith("_x"):
                    hat_map["x"][hat_key] = mapping
                elif hat_key.endswith("_y"):
                    hat_map["y"][hat_key] = mapping

    return default_joystick_index, axis_map, button_map, hat_map


def prune_joystick_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> None:
    """Drop INI mappings for controls this joystick does not have (prevents ghost axes on CH5+)."""
    if joystick is None:
        return
    n_ax = joystick.get_numaxes()
    n_btn = joystick.get_numbuttons()
    n_hat = joystick.get_numhats()
    removed = 0

    for key in list(axis_map.keys()):
        if not key.startswith("axis_"):
            continue
        tail = key[5:]
        if not tail.isdigit():
            continue
        if int(tail) >= n_ax:
            del axis_map[key]
            removed += 1

    for key in list(button_map.keys()):
        if not key.startswith("button_"):
            continue
        tail = key[7:]
        if not tail.isdigit():
            continue
        if int(tail) >= n_btn:
            del button_map[key]
            removed += 1

    for side in ("x", "y"):
        hm = hat_map[side]
        for key in list(hm.keys()):
            parts = key.split("_")
            if len(parts) != 3 or parts[0] != "hat" or parts[2] != side:
                continue
            if not parts[1].isdigit():
                continue
            if int(parts[1]) >= n_hat:
                del hm[key]
                removed += 1

    if removed:
        _LOG.info(
            "Removed %d controller_map mapping(s) not present on this device (frees CH5+ for real inputs).",
            removed,
        )


def _used_channels(axis_map: Dict, button_map: Dict, hat_map: Dict) -> set[int]:
    u: set[int] = set()
    for m in axis_map.values():
        u.add(m["channel"])
    for m in button_map.values():
        u.add(m["channel"])
    for m in hat_map["x"].values():
        u.add(m["channel"])
    for m in hat_map["y"].values():
        u.add(m["channel"])
    return u


def apply_rc_preset_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> int:
    """Typical gamepad layout: axis 0–3 → CH1–4 (roll/pitch/yaw/throttle), buttons 0–3 → CH5–8 when free."""
    if joystick is None:
        return 0
    used = _used_channels(axis_map, button_map, hat_map)
    added = 0
    n_ax = joystick.get_numaxes()
    n_btn = joystick.get_numbuttons()
    for i in range(min(4, n_ax)):
        key = f"axis_{i}"
        ch = i + 1
        if key not in axis_map and ch not in used:
            axis_map[key] = {"channel": ch, "invert": False}
            used.add(ch)
            added += 1
    for i in range(min(4, n_btn)):
        key = f"button_{i}"
        ch = 5 + i
        if key not in button_map and ch not in used:
            button_map[key] = {"channel": ch, "invert": False}
            used.add(ch)
            added += 1
    return added


def merge_default_joystick_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> int:
    """Map any physical control not listed in controller_map to the next free RC channel (1..16).

    Same idea as assigning inputs in ``minirex_pygame_no_config_file.py``: axes, buttons, and hat
    directions that have no INI entry still drive spare outputs so CH5+ are not stuck at 1500.
    Explicit INI mappings are kept; only missing keys get auto channels.
    """
    if joystick is None:
        return 0

    def used_channels() -> set[int]:
        return _used_channels(axis_map, button_map, hat_map)

    used = used_channels()

    def next_free() -> Optional[int]:
        for c in range(1, 17):
            if c not in used:
                return c
        return None

    added = 0

    for i in range(joystick.get_numaxes()):
        key = f"axis_{i}"
        if key not in axis_map:
            c = next_free()
            if c is None:
                break
            axis_map[key] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    for i in range(joystick.get_numbuttons()):
        key = f"button_{i}"
        if key not in button_map:
            c = next_free()
            if c is None:
                break
            button_map[key] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    for i in range(joystick.get_numhats()):
        kx = f"hat_{i}_x"
        if kx not in hat_map["x"]:
            c = next_free()
            if c is None:
                return added
            hat_map["x"][kx] = {"channel": c, "invert": False}
            used.add(c)
            added += 1
        ky = f"hat_{i}_y"
        if ky not in hat_map["y"]:
            c = next_free()
            if c is None:
                return added
            hat_map["y"][ky] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    return added


def open_joystick(
    index: Optional[int],
    previous: Optional[pygame.joystick.Joystick] = None,
) -> Optional[pygame.joystick.Joystick]:
    """Open a joystick by index. Quits ``previous`` only — avoids pygame.joystick.quit()/init() each time (hangs on macOS + Tk)."""
    if previous is not None:
        try:
            previous.quit()
        except Exception:
            pass
    if index is None or index < 0 or index >= pygame.joystick.get_count():
        return None
    if not pygame.joystick.get_init():
        pygame.joystick.init()
    j = pygame.joystick.Joystick(index)
    j.init()
    return j


def joy_menu_values() -> List[str]:
    n = pygame.joystick.get_count()
    if n == 0:
        return ["(no joystick)"]
    return [f"{i}: {pygame.joystick.Joystick(i).get_name()}" for i in range(n)]
